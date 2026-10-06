"""HTTP 路由：页面 + JSON API + 核验包导出。

所有名单变更与封存/揭晓接口都在 ``db.transaction()``（BEGIN IMMEDIATE）内
重新检查状态并读写名单，然后一次提交；请求之间由 SQLite 写锁串行。
"""
import io
import json
import os
import zipfile

from flask import (
    Blueprint,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

from . import rules
from . import services
from .db import get_db, transaction
from .services import ApiError

bp = Blueprint("main", __name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 页面

@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/rounds/<round_id>")
def round_page(round_id):
    return render_template("round.html", round_id=round_id)


@bp.get("/verify")
def verify_page():
    return render_template("verify.html")


# ---------------------------------------------------------------- API 工具

def err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def get_round_or_404(round_id: str):
    return services.get_round(get_db(), round_id)


def round_public_dict(row, *, include_snapshot: bool = True) -> dict:
    db = get_db()
    participants = services.participant_rows(db, row["id"])
    winners = json.loads(row["winners_json"]) if row["winners_json"] else None
    data = {
        "id": row["id"],
        "title": row["title"],
        "prizeCount": row["prize_count"],
        "commitment": row["commit_hex"],
        "status": row["status"],
        "listDigest": row["list_digest_hex"],
        "participantCount": row["participant_count"]
        if row["participant_count"] is not None
        else len(participants),
        "failedAttempts": row["failed_attempts"],
        "createdAt": row["created_at"],
        "sealedAt": row["sealed_at"],
        "revealedAt": row["revealed_at"],
        "seed": row["seed_hex"],
        "winners": winners,
        "participants": participants,
    }
    if include_snapshot and row["snapshot"] is not None:
        data["snapshot"] = row["snapshot"]
    return data


def _error_response(e: ApiError):
    return jsonify({"ok": False, "error": e.message, **e.extra}), e.status


# ---------------------------------------------------------------- 轮次 API

@bp.post("/api/rounds")
def create_round():
    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        with transaction(db):
            row = services.create_round_tx(
                db, body.get("title"), body.get("prizeCount"),
                body.get("commitment"),
            )
    except ApiError as e:
        return _error_response(e)
    return jsonify({"ok": True, "round": round_public_dict(row)})


@bp.get("/api/rounds")
def list_rounds():
    rows = get_db().execute(
        "SELECT * FROM rounds ORDER BY created_at DESC, id DESC"
    ).fetchall()
    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "title": r["title"],
                "prizeCount": r["prize_count"],
                "commitment": r["commit_hex"],
                "status": r["status"],
                "listDigest": r["list_digest_hex"],
                "participantCount": r["participant_count"],
                "createdAt": r["created_at"],
                "revealedAt": r["revealed_at"],
            }
        )
    return jsonify({"ok": True, "rounds": items})


@bp.get("/api/rounds/<round_id>")
def get_round(round_id):
    row = get_round_or_404(round_id)
    if row is None:
        return err("轮次不存在", 404)
    return jsonify({"ok": True, "round": round_public_dict(row)})


# ---------------------------------------------------------------- 名单 API

@bp.put("/api/rounds/<round_id>/participants")
def replace_participants(round_id):
    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        with transaction(db):
            participants, count = services.replace_participants_tx(
                db, round_id, body.get("participants")
            )
    except ApiError as e:
        return _error_response(e)
    return jsonify({"ok": True, "participants": participants, "count": count})


@bp.post("/api/rounds/<round_id>/participants")
def add_participant(round_id):
    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        with transaction(db):
            participants = services.add_participant_tx(
                db, round_id, body.get("id")
            )
    except ApiError as e:
        return _error_response(e)
    return jsonify({"ok": True, "participants": participants})


@bp.delete("/api/rounds/<round_id>/participants/<entry_id>")
def delete_participant(round_id, entry_id):
    db = get_db()
    try:
        with transaction(db):
            participants = services.delete_participant_tx(db, round_id, entry_id)
    except ApiError as e:
        return _error_response(e)
    return jsonify({"ok": True, "participants": participants})


# ---------------------------------------------------------------- 封存 / 揭晓

@bp.post("/api/rounds/<round_id>/seal")
def seal_round(round_id):
    db = get_db()
    try:
        with transaction(db):
            row, already_sealed = services.seal_round_tx(db, round_id)
    except ApiError as e:
        return _error_response(e)
    if already_sealed:
        return jsonify({"ok": True, "alreadySealed": True,
                        "round": round_public_dict(row)})
    return jsonify(
        {"ok": True, "round": round_public_dict(row)}
    )


@bp.post("/api/rounds/<round_id>/reveal")
def reveal_round(round_id):
    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        with transaction(db):
            result = services.reveal_round_tx(db, round_id, body.get("seed"))
    except ApiError as e:
        return _error_response(e)

    # 重复揭晓：不重抽、不覆盖，原样返回既有结果
    if result.get("already_revealed"):
        return jsonify(
            {
                "ok": True,
                "alreadyRevealed": True,
                "message": "该轮次已揭晓，结果固定，重复提交不会重抽",
                "round": round_public_dict(result["row"]),
            }
        )

    if result.get("commit_mismatch"):
        return jsonify(
            {
                "ok": False,
                "commitMismatch": True,
                "error": "种子与事先留存的承诺不匹配，轮次保持封存状态",
                "round": round_public_dict(result["row"]),
            }
        )

    return jsonify(
        {"ok": True, "round": round_public_dict(result["row"])}
    )


# ---------------------------------------------------------------- 核验包

def build_manifest(row) -> dict:
    entries = services.participant_rows(get_db(), row["id"])
    if row["snapshot"] is not None:
        entries_ids = row["snapshot"].split("\n")
        count = row["participant_count"]
    else:
        entries_ids = sorted((p["id"] for p in entries),
                             key=lambda e: e.encode("utf-8"))
        count = len(entries_ids)

    return {
        "format": "public-lottery-verification-v1",
        "ruleId": rules.RULE_ID,
        "round": {
            "id": row["id"],
            "title": row["title"],
            "prizeCount": row["prize_count"],
            "status": row["status"],
            "commitment": row["commit_hex"],
            "seed": row["seed_hex"],
            "listDigest": row["list_digest_hex"],
            "createdAt": row["created_at"],
            "sealedAt": row["sealed_at"],
            "revealedAt": row["revealed_at"],
        },
        "participants": entries_ids,
        "participantCount": count,
        "winners": json.loads(row["winners_json"]) if row["winners_json"] else None,
        "notes": (
            "用 verify/verify.py 对本文件独立重算校验；切勿只比对本包内 winners 字段。"
            "封存后（含未揭晓）即可导出并对照事先留存的承诺与名单摘要。"
        ),
    }


def _read_verifier() -> bytes:
    with open(os.path.join(ROOT_DIR, "verify", "verify.py"), "rb") as f:
        return f.read()


def _read_rules_doc() -> bytes:
    with open(os.path.join(ROOT_DIR, "RULES.md"), "rb") as f:
        return f.read()


@bp.get("/api/rounds/<round_id>/package.json")
def package_json(round_id):
    row = get_round_or_404(round_id)
    if row is None:
        return err("轮次不存在", 404)
    manifest = build_manifest(row)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    return Response(
        payload,
        mimetype="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="lottery-{round_id}-package.json"'
            )
        },
    )


@bp.get("/api/rounds/<round_id>/package.zip")
def package_zip(round_id):
    row = get_round_or_404(round_id)
    if row is None:
        return err("轮次不存在", 404)
    manifest = build_manifest(row)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"lottery-{row['id']}/package.json", manifest_bytes)
        zf.writestr(f"lottery-{row['id']}/list.txt",
                    ("\n".join(manifest["participants"]) + "\n").encode("utf-8"))
        zf.writestr(f"lottery-{row['id']}/RULES.md", _read_rules_doc())
        zf.writestr(f"lottery-{row['id']}/verify.py", _read_verifier())
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="lottery-{row["id"]}-package.zip"'
            )
        },
    )


# ---------------------------------------------------------------- 静态资源

@bp.get("/verifier/verify.py")
def download_verifier():
    return send_from_directory(
        os.path.join(ROOT_DIR, "verify"), "verify.py", mimetype="text/x-python"
    )


@bp.get("/RULES.md")
def download_rules_doc():
    return send_from_directory(ROOT_DIR, "RULES.md", mimetype="text/markdown")

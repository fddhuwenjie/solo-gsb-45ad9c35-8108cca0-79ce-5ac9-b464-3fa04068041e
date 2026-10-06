"""HTTP 路由：页面 + JSON API + 核验包导出。"""
import io
import json
import os
import secrets
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
from .db import get_db, now_iso

bp = Blueprint("main", __name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_PRIZE = 10000


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
    return get_db().execute(
        "SELECT * FROM rounds WHERE id=?", (round_id,)
    ).fetchone()


def participant_rows(round_id: str) -> list[dict]:
    rows = get_db().execute(
        "SELECT entry_id, position FROM participants WHERE round_id=? "
        "ORDER BY position, entry_id",
        (round_id,),
    ).fetchall()
    return [{"id": r["entry_id"], "position": r["position"]} for r in rows]


def round_public_dict(row, *, include_snapshot: bool = True) -> dict:
    db = get_db()
    participants = participant_rows(row["id"])
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


# ---------------------------------------------------------------- 轮次 API

@bp.post("/api/rounds")
def create_round():
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    prize_count = body.get("prizeCount")
    commitment = (body.get("commitment") or "").strip().lower()

    if not title:
        return err("请填写轮次名称")
    if not isinstance(prize_count, int) or isinstance(prize_count, bool):
        return err("中奖名额必须是整数")
    if not 1 <= prize_count <= MAX_PRIZE:
        return err(f"中奖名额必须在 1 到 {MAX_PRIZE} 之间")
    if not rules.is_hex64(commitment):
        return err("承诺必须是 64 位十六进制 SHA-256 摘要")

    round_id = secrets.token_hex(6)
    db = get_db()
    db.execute(
        "INSERT INTO rounds (id,title,prize_count,commit_hex,created_at) "
        "VALUES (?,?,?,?,?)",
        (round_id, title, prize_count, commitment, now_iso()),
    )
    db.commit()
    row = get_round_or_404(round_id)
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

def _require_open(row):
    if row is None:
        return err("轮次不存在", 404)
    if row["status"] != "open":
        return err("名单已封存，此后拒绝任何修改", 409)
    return None


@bp.put("/api/rounds/<round_id>/participants")
def replace_participants(round_id):
    row = get_round_or_404(round_id)
    error = _require_open(row)
    if error:
        return error

    body = request.get_json(silent=True) or {}
    raw = body.get("participants")
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        return err("participants 必须是字符串数组")

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        entry = rules.normalize_entry(item)
        if not entry:
            continue
        if entry in seen:
            return err(f"编号重复：{entry}")
        if "\n" in entry or "\r" in entry:
            return err(f"编号不能包含换行符：{entry}")
        if len(entry) > 200:
            return err(f"编号过长（>200 字符）：{entry}")
        seen.add(entry)
        cleaned.append(entry)

    db = get_db()
    db.execute("DELETE FROM participants WHERE round_id=?", (round_id,))
    db.executemany(
        "INSERT INTO participants (round_id,entry_id,position,added_at) "
        "VALUES (?,?,?,?)",
        [(round_id, e, i, now_iso()) for i, e in enumerate(cleaned)],
    )
    db.commit()
    return jsonify(
        {"ok": True, "participants": participant_rows(round_id), "count": len(cleaned)}
    )


@bp.post("/api/rounds/<round_id>/participants")
def add_participant(round_id):
    row = get_round_or_404(round_id)
    error = _require_open(row)
    if error:
        return error

    body = request.get_json(silent=True) or {}
    entry = rules.normalize_entry(body.get("id") or "")
    if not entry:
        return err("请输入编号")
    if "\n" in entry or "\r" in entry or len(entry) > 200:
        return err("编号不合法")

    db = get_db()
    exists = db.execute(
        "SELECT 1 FROM participants WHERE round_id=? AND entry_id=?",
        (round_id, entry),
    ).fetchone()
    if exists:
        return err(f"编号已存在：{entry}", 409)
    pos = db.execute(
        "SELECT COALESCE(MAX(position)+1,0) FROM participants WHERE round_id=?",
        (round_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO participants (round_id,entry_id,position,added_at) "
        "VALUES (?,?,?,?)",
        (round_id, entry, pos, now_iso()),
    )
    db.commit()
    return jsonify({"ok": True, "participants": participant_rows(round_id)})


@bp.delete("/api/rounds/<round_id>/participants/<entry_id>")
def delete_participant(round_id, entry_id):
    row = get_round_or_404(round_id)
    error = _require_open(row)
    if error:
        return error
    db = get_db()
    db.execute(
        "DELETE FROM participants WHERE round_id=? AND entry_id=?",
        (round_id, entry_id),
    )
    db.commit()
    return jsonify({"ok": True, "participants": participant_rows(round_id)})


# ---------------------------------------------------------------- 封存 / 揭晓

@bp.post("/api/rounds/<round_id>/seal")
def seal_round(round_id):
    row = get_round_or_404(round_id)
    if row is None:
        return err("轮次不存在", 404)
    if row["status"] == "revealed":
        return err("轮次已揭晓，无法封存", 409)
    if row["status"] == "sealed":
        return jsonify({"ok": True, "alreadySealed": True,
                        "round": round_public_dict(row)})

    entries = [p["id"] for p in participant_rows(round_id)]
    if len(entries) < row["prize_count"]:
        return err(
            f"人数不足：当前 {len(entries)} 人，中奖名额 {row['prize_count']} 个，"
            "封存要求人数不少于名额"
        )

    snapshot = rules.canonical_snapshot(entries)
    digest = rules.sha256_hex(snapshot.encode("utf-8"))
    ts = now_iso()
    db = get_db()
    db.execute(
        "UPDATE rounds SET status='sealed', snapshot=?, list_digest_hex=?, "
        "participant_count=?, sealed_at=? WHERE id=?",
        (snapshot, digest, len(entries), ts, round_id),
    )
    db.commit()
    return jsonify(
        {"ok": True, "round": round_public_dict(get_round_or_404(round_id))}
    )


@bp.post("/api/rounds/<round_id>/reveal")
def reveal_round(round_id):
    row = get_round_or_404(round_id)
    if row is None:
        return err("轮次不存在", 404)

    # 重复揭晓：不重抽、不覆盖，原样返回既有结果
    if row["status"] == "revealed":
        return jsonify(
            {
                "ok": True,
                "alreadyRevealed": True,
                "message": "该轮次已揭晓，结果固定，重复提交不会重抽",
                "round": round_public_dict(row),
            }
        )
    if row["status"] != "sealed":
        return err("轮次尚未封存", 409)

    body = request.get_json(silent=True) or {}
    seed = (body.get("seed") or "").strip().lower()
    db = get_db()

    # 承诺不匹配：保持封存状态
    if not rules.seed_matches_commit(seed, row["commit_hex"]):
        db.execute(
            "UPDATE rounds SET failed_attempts=failed_attempts+1 WHERE id=?",
            (round_id,),
        )
        db.commit()
        refreshed = get_round_or_404(round_id)
        return jsonify(
            {
                "ok": False,
                "commitMismatch": True,
                "error": "种子与事先留存的承诺不匹配，轮次保持封存状态",
                "round": round_public_dict(refreshed),
            }
        )

    entries = row["snapshot"].split("\n") if row["snapshot"] else []
    winners = rules.draw_winners(entries, seed, row["prize_count"])
    ts = now_iso()
    db.execute(
        "UPDATE rounds SET status='revealed', seed_hex=?, winners_json=?, "
        "revealed_at=? WHERE id=?",
        (seed, json.dumps(winners, ensure_ascii=False), ts, round_id),
    )
    db.commit()
    return jsonify(
        {"ok": True, "round": round_public_dict(get_round_or_404(round_id))}
    )


# ---------------------------------------------------------------- 核验包

def build_manifest(row) -> dict:
    entries = participant_rows(row["id"])
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

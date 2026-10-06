#!/usr/bin/env python3
"""现场抽签核验包独立校验工具（sha256-argmax-v1）。

用法：
    python3 verify.py package.json
    python3 verify.py lottery-<轮次编号>-package.zip
    python3 verify.py package.json --commitment <事先留存的承诺> \\
                                   --list-digest <事先留存的名单摘要>

本脚本只使用 Python 3 标准库。它不会相信核验包里的 winners 字段：
中奖编号一律由种子和名单按公开规则重新计算后逐项比对。
发现任何不符，退出码为 1；全部通过退出码为 0。
"""
import argparse
import hashlib
import io
import json
import sys
import zipfile

RULE_ID = "sha256-argmax-v1"
COMMIT_PREFIX = "COMMIT:"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_snapshot(entries):
    return "\n".join(sorted(entries, key=lambda e: e.encode("utf-8")))


def draw_winners(entries, seed_hex, prize_count):
    pool = sorted(entries, key=lambda e: e.encode("utf-8"))
    winners = []
    for i in range(prize_count):
        best_id, best_score = None, -1
        for entry_id in pool:
            msg = f"{RULE_ID}|{seed_hex}|{entry_id}|{i}".encode("utf-8")
            score = int.from_bytes(hashlib.sha256(msg).digest(), "big")
            if score > best_score:  # 平局保留字节序靠前者
                best_score, best_id = score, entry_id
        winners.append(best_id)
        pool.remove(best_id)
    return winners


def load_package(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.endswith("package.json")]
            if not names:
                raise ValueError("zip 中找不到 package.json")
            return json.loads(zf.read(names[0]).decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def verify(pkg, expected_commitment=None, expected_digest=None):
    errors, checks = [], []

    def ok(msg):
        checks.append(msg)

    def fail(msg):
        errors.append(msg)

    # ---- 结构 ----
    if pkg.get("format") != "public-lottery-verification-v1":
        fail(f"format 字段不符：{pkg.get('format')!r}")
    rule_id = pkg.get("ruleId")
    if rule_id != RULE_ID:
        fail(f"规则标识不符：包内为 {rule_id!r}，本核验器只认 {RULE_ID}")

    rnd = pkg.get("round", {})
    title = rnd.get("title", "?")
    prize_count = rnd.get("prizeCount")
    commitment = (rnd.get("commitment") or "").lower()
    seed = (rnd.get("seed") or "").strip().lower() or None
    stored_digest = (rnd.get("listDigest") or "").lower() or None
    status = rnd.get("status")
    entries = pkg.get("participants")
    stored_winners = pkg.get("winners")

    print(f"轮次：{title}（{rnd.get('id','?')}）")
    print(f"状态：{status}    名额：{prize_count}    人数：{pkg.get('participantCount')}")
    print(f"规则：{rule_id}")
    print("-" * 68)

    if not isinstance(prize_count, int) or isinstance(prize_count, bool) or prize_count < 1:
        fail(f"中奖名额非法：{prize_count!r}")
        prize_count = None
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        fail("participants 必须是字符串数组")
        return errors, checks

    # ---- 名单本身：空编号 / 换行 / 重复 / 顺序 ----
    bad_ids = [e for e in entries if not e.strip() or e != e.strip()
               or "\n" in e or "\r" in e]
    if bad_ids:
        fail(f"名单含空编号或含换行/首尾空白的编号：{bad_ids[:5]}")
    dup = sorted({e for e in entries if entries.count(e) > 1})
    if dup:
        fail(f"名单存在重复编号：{dup}")

    want_order = [e for e in sorted(entries, key=lambda e: e.encode("utf-8"))]
    if entries != want_order:
        mismatch_pos = next(
            (i for i, (a, b) in enumerate(zip(entries, want_order)) if a != b),
            min(len(entries), len(want_order)),
        )
        fail(
            "名单顺序不是规范的 UTF-8 字节序："
            f"首个偏差在第 {mismatch_pos + 1} 项 "
            f"（包内 {entries[mismatch_pos] if mismatch_pos < len(entries) else '∅'}，"
            f"应为 {want_order[mismatch_pos] if mismatch_pos < len(want_order) else '∅'}）"
        )
    else:
        ok("名单为按 UTF-8 字节序排序的规范名单，且编号无重复")

    # ---- 名单摘要：重算 vs 包内 ----
    recomputed_digest = sha256_hex(canonical_snapshot(entries).encode("utf-8"))
    if stored_digest is None:
        fail("核验包缺少 listDigest（名单摘要）")
    elif recomputed_digest != stored_digest:
        fail(
            "名单摘要不符：包内名单重算为\n"
            f"        {recomputed_digest}\n"
            f"  包内记录为 {stored_digest}"
        )
    else:
        ok(f"名单摘要核对一致：{stored_digest}")

    # ---- 事先留存的摘要 ----
    if expected_digest is not None:
        expected_digest = expected_digest.strip().lower()
        if expected_digest != recomputed_digest:
            fail(
                "事先留存的名单摘要与包内名单重算结果不符：\n"
                f"        事先留存 {expected_digest}\n"
                f"        重新计算 {recomputed_digest}"
            )
        elif stored_digest and expected_digest != stored_digest:
            fail("事先留存的名单摘要与包内 listDigest 字段不符")
        else:
            ok("与事先留存的名单摘要一致")

    # ---- 承诺格式 ----
    if len(commitment) != 64 or any(c not in "0123456789abcdef" for c in commitment):
        fail(f"承诺不是 64 位十六进制：{commitment!r}")

    # ---- 事先留存的承诺 ----
    if expected_commitment is not None:
        expected_commitment = expected_commitment.strip().lower()
        if expected_commitment != commitment:
            fail(
                f"事先留存的承诺与包内承诺不符：\n"
                f"        事先留存 {expected_commitment}\n"
                f"        包内记录 {commitment}"
            )
        else:
            ok("与事先留存的承诺一致")

    # ---- 种子—承诺 ----
    if seed is None:
        print("提示：该包处于封存（未揭晓）状态，暂无种子与结果可校验。")
        if status != "sealed":
            fail(f"包内无种子，但状态为 {status!r} 而非 'sealed'")
        return errors, checks

    if len(seed) != 64 or any(c not in "0123456789abcdef" for c in seed):
        fail(f"种子不是 64 位十六进制：{seed!r}")
        return errors, checks

    recomputed_commit = sha256_hex((COMMIT_PREFIX + seed).encode("utf-8"))
    if recomputed_commit != commitment:
        fail(
            "种子与承诺不匹配：\n"
            f"        由种子重算 {recomputed_commit}\n"
            f"        包内承诺     {commitment}"
        )
    else:
        ok("种子与承诺匹配：SHA256('COMMIT:'+seed) 与承诺一致")

    # ---- 状态与结果字段 ----
    if status != "revealed":
        fail(f"包内有种子/结果，但状态为 {status!r}，应为 'revealed'")

    # ---- 关键：按规则从种子+名单独立重抽，绝不只比对包内 winners ----
    if prize_count is not None and prize_count <= len(entries) and len(dup) == 0:
        recalc = draw_winners(entries, seed, prize_count)
        if stored_winners is None:
            fail("核验包缺少 winners 字段")
        elif not isinstance(stored_winners, list):
            fail("winners 字段不是数组")
        else:
            if len(stored_winners) != prize_count:
                fail(f"中奖数量 {len(stored_winners)} 不等于名额 {prize_count}")
            if len(set(stored_winners)) != len(stored_winners):
                fail("包内中奖编号存在重复")
            outside = [w for w in stored_winners if w not in entries]
            if outside:
                fail(f"包内中奖编号不在封存名单中：{outside}")
            if stored_winners != recalc:
                diffs = []
                for i, (a, b) in enumerate(zip(stored_winners, recalc)):
                    if a != b:
                        diffs.append(f"第 {i + 1} 位：包内 {a!r}，重算 {b!r}")
                if len(stored_winners) != len(recalc):
                    diffs.append(
                        f"数量不同：包内 {len(stored_winners)}，重算 {len(recalc)}"
                    )
                fail("按公开规则由种子与名单重算的结果与包内结果不一致：\n    "
                     + "\n    ".join(diffs[:20]))
            else:
                ok(f"按规则独立重算 {prize_count} 个中奖编号，与包内结果完全一致")
        print("-" * 68)
        print("独立重算的中奖编号（按产生顺序）：")
        for i, w in enumerate(recalc, 1):
            print(f"  {i:>3}. {w}")
    elif prize_count is not None and prize_count > len(entries):
        fail(f"人数 {len(entries)} 少于名额 {prize_count}，按规则本不该封存/揭晓")

    return errors, checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="现场抽签核验包独立校验")
    parser.add_argument("package", help="package.json 或导出的 .zip 核验包")
    parser.add_argument("--commitment", help="事先留存的承诺（SHA-256 hex）")
    parser.add_argument("--list-digest", help="事先留存的名单摘要（SHA-256 hex）")
    args = parser.parse_args(argv)

    try:
        pkg = load_package(args.package)
    except Exception as e:  # noqa: BLE001
        print(f"无法读取核验包：{e}")
        return 2

    errors, checks = verify(pkg, args.commitment, args.list_digest)

    print("-" * 68)
    for c in checks:
        print(f"  [通过] {c}")
    if errors:
        print()
        for e in errors:
            for line in str(e).splitlines():
                print(f"  [不符] {line}")
        print()
        print(f"结论：发现 {len(errors)} 处不符，核验未通过。")
        return 1
    print()
    print("结论：全部核验通过。中奖结果由公开规则、种子与封存名单确定性地推出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

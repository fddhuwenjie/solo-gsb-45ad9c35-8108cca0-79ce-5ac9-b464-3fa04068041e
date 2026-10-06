"""公开的确定性散列规则（唯一事实来源，Python 端）。

规则标识: sha256-argmax-v1

1. 承诺
   秘密种子 seed 为 64 个十六进制字符（256 位随机数的小写 hex 表示）。
   commitment = SHA256( "COMMIT:" + seed ) 的十六进制摘要。

2. 规范名单
   每个编号取去除首尾空白后的 UTF-8 字符串；编号互不相同。
   名单按编号的 UTF-8 字节序升序排序；
   规范快照 snapshot = "\n".join(排序后的编号)；
   名单摘要 list_digest = SHA256(snapshot 的 UTF-8 编码)。

3. 抽奖（从空列表开始，逐轮扩充；共 prize_count 轮，索引 i 从 0 起）
   对尚未中奖的每个编号 id：
       msg  = "sha256-argmax-v1|" + seed + "|" + id + "|" + str(i)
       score = 将 SHA256(msg 的 UTF-8 编码) 的 32 字节按大端序解释为无符号整数
   取得分最高的编号；若分数相同（概率约 2^-256），取规范顺序靠前者。
   第 i 轮中奖编号即该编号，随后将其移出候选集合。
   全程不做取模，因而不存在取模偏置；结果只依赖 seed 与最终名单。
"""
import hashlib
import hmac

RULE_ID = "sha256-argmax-v1"
COMMIT_PREFIX = "COMMIT:"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utf8(s: str) -> bytes:
    return s.encode("utf-8")


def normalize_entry(entry_id: str) -> str:
    return entry_id.strip()


def is_hex64(s: str) -> bool:
    return (
        isinstance(s, str)
        and len(s) == 64
        and all(c in "0123456789abcdef" for c in s.lower())
    )


def canonical_snapshot(entries: list[str]) -> str:
    """规范名单快照：编号按 UTF-8 字节序排序，换行连接。"""
    return "\n".join(sorted(entries, key=lambda e: e.encode("utf-8")))


def list_digest(entries: list[str]) -> str:
    return sha256_hex(utf8(canonical_snapshot(entries)))


def commit_for_seed(seed_hex: str) -> str:
    return sha256_hex(utf8(COMMIT_PREFIX + seed_hex.strip().lower()))


def seed_matches_commit(seed_hex: str, commit_hex: str) -> bool:
    if not is_hex64(seed_hex):
        return False
    return hmac.compare_digest(
        commit_for_seed(seed_hex), commit_hex.strip().lower()
    )


def draw_winners(entries: list[str], seed_hex: str, prize_count: int) -> list[str]:
    """按公开规则确定性地选出不重复中奖编号。

    调用方负责保证 0 < prize_count <= len(entries) 且编号互不相同。
    """
    if not 0 < prize_count <= len(entries):
        raise ValueError("prize_count 必须满足 0 < prize_count <= 人数")

    pool = sorted(entries, key=lambda e: e.encode("utf-8"))
    winners: list[str] = []
    for i in range(prize_count):
        best_id = None
        best_score = -1
        for entry_id in pool:
            msg = f"{RULE_ID}|{seed_hex}|{entry_id}|{i}"
            score = int.from_bytes(hashlib.sha256(utf8(msg)).digest(), "big")
            if score > best_score:  # 严格大于：平局保留规范顺序靠前者
                best_score = score
                best_id = entry_id
        winners.append(best_id)
        pool.remove(best_id)
    return winners

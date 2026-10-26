"""术语表同义词归一（幂等迁移）：合并重复术语、清理冲突别名、补充口语同义词。

规则见 app/synonyms.py。合并/清理后需要重建派生数据：
    python scripts/normalize_terms.py
    python scripts/backfill_entities.py   # chunks.entities 重新打标（合并后的规范词）
    python scripts/mine_edges.py          # term_edges 按新术语表重建
    python scripts/sync_term_mappings.py  # 别名 → 查询词映射
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.synonyms import MERGES, normalize_alias_map  # noqa: E402


def main() -> None:
    conn = psycopg2.connect(**settings.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT term, type, aliases FROM terms ORDER BY term")
            rows = cur.fetchall()
        alias_map = {r[0]: list(r[2] or []) for r in rows}
        normalized, merged = normalize_alias_map(alias_map)
        changed = [
            t for t, aliases in normalized.items()
            if t in alias_map and aliases != alias_map[t]
        ]
        with conn.cursor() as cur:
            if merged:
                cur.execute("DELETE FROM terms WHERE term = ANY(%s)", (merged,))
            for t in changed:
                cur.execute(
                    "UPDATE terms SET aliases=%s WHERE term=%s",
                    (json.dumps(normalized[t], ensure_ascii=False), t),
                )
        conn.commit()

        # 复核：冲突是否清零
        with conn.cursor() as cur:
            cur.execute("SELECT term, aliases FROM terms")
            rows = cur.fetchall()
        canonical = {r[0] for r in rows}
        conflicts = [
            (t, a) for t, al in rows for a in (al or []) if a in canonical and a != t
        ]
        owners: dict[str, list[str]] = {}
        for t, al in rows:
            for a in (al or []):
                owners.setdefault(a, []).append(t)
        multi = {a: os for a, os in owners.items() if len(os) > 1}

        print(f"术语表：{len(rows)} 条（原 {len(alias_map)}）")
        print(f"合并删除 {len(merged)} 条：{merged}")
        print(f"别名更新 {len(changed)} 条")
        print(f"剩余冲突：别名=独立术语 {len(conflicts)} 处，重复别名 {len(multi)} 处")
        if conflicts:
            print("  ", conflicts[:10])
        if multi:
            print("  ", multi)
        print("\n合并后的规范词别名示例：")
        for t in ("手淫", "意淫", "看黄", "淫梦", "神经症", "焦虑症", "觉察"):
            cur = conn.cursor()
            cur.execute("SELECT aliases FROM terms WHERE term=%s", (t,))
            row = cur.fetchone()
            print(f"  {t}: {' / '.join(row[0]) if row else '（无此词条）'}")
    finally:
        conn.close()

    if MERGES:
        print("\n提示：改动后请重跑 backfill_entities.py / mine_edges.py / sync_term_mappings.py")


if __name__ == "__main__":
    main()

"""把术语表别名同步为查询词映射（两处数据同源，避免口径不一致）。

规则见 app/synonyms.mapping_candidates：
- 单字/单字母、与独立术语重名、是其他术语子串、是目标词子串 → 跳过
- 语义歧义别名（MAPPING_BLOCKLIST）→ 只留作实体别名
- 已有映射（source_term, match_type 唯一）→ 不覆盖

幂等：可反复运行，只补缺。
    python scripts/sync_term_mappings.py           # 同步
    python scripts/sync_term_mappings.py --dry-run # 只看候选与跳过原因
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.synonyms import mapping_candidates  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="术语别名 → 查询词映射同步")
    ap.add_argument("--dry-run", action="store_true", help="只打印候选，不落库")
    args = ap.parse_args()

    conn = psycopg2.connect(**settings.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT term, aliases FROM terms WHERE enabled ORDER BY term")
            terms = {r[0]: list(r[1] or []) for r in cur.fetchall()}
        ok, skipped = mapping_candidates(terms)

        created = existing = 0
        if not args.dry_run:
            with conn.cursor() as cur:
                for source, target, match_type in ok:
                    cur.execute(
                        "INSERT INTO query_term_mappings (source_term, target_term, match_type,"
                        " priority, enabled, remark) VALUES (%s,%s,%s,%s,TRUE,%s)"
                        " ON CONFLICT (source_term, match_type) DO NOTHING",
                        (source, target, match_type, 100, "术语表别名同步"),
                    )
                    if cur.rowcount:
                        created += 1
                    else:
                        existing += 1
            conn.commit()

        print(f"候选映射 {len(ok)} 条：新增 {created if not args.dry_run else '-'}，"
              f"已存在 {existing if not args.dry_run else '-'}，跳过 {len(skipped)} 条")
        if args.dry_run:
            print("\n候选（source → target / 类型）:")
            for s, t, mt in ok:
                print(f"  {s} → {t} (type {mt})")
        if skipped:
            print("\n跳过明细:")
            for s, t, why in skipped:
                print(f"  {s} → {t}：{why}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

"""知识库构建管线：EPUB 解析 → 分块 → 去重 → 分词 → PostgreSQL（→ 可选向量索引）。

用法（在 backend 目录下）：
    python scripts/build_kb.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import psycopg2

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.chunker import Chunk, chunk_book  # noqa: E402
from app.config import settings  # noqa: E402
from app.dedup import dedup  # noqa: E402
from app.epub_parser import _IdAlloc, parse_epub  # noqa: E402
from app.glossary import tokenize  # noqa: E402

SCHEMA_BODY = """
CREATE TABLE books(id TEXT PRIMARY KEY, title TEXT, author TEXT,
                   doc_count INTEGER, char_count INTEGER);
CREATE TABLE docs(id TEXT PRIMARY KEY, book_id TEXT, seq INTEGER, file TEXT,
                  title TEXT, author TEXT, char_count INTEGER, para_count INTEGER);
CREATE TABLE paras(id TEXT PRIMARY KEY, doc_id TEXT, seq INTEGER, kind TEXT,
                   text TEXT, elem_id TEXT);
CREATE TABLE toc(id TEXT PRIMARY KEY, book_id TEXT, doc_id TEXT, para_id TEXT,
                 title TEXT, level INTEGER, seq INTEGER);
CREATE TABLE chunks(id TEXT PRIMARY KEY, book_id TEXT, doc_id TEXT, seq INTEGER,
                    para_ids TEXT, breadcrumb TEXT, chapter_title TEXT,
                    section_title TEXT, author TEXT, doc_type TEXT,
                    text TEXT, tokens TEXT, dup_of TEXT,
                    entities JSONB NOT NULL DEFAULT '{}');
CREATE INDEX idx_paras_doc ON paras(doc_id);
CREATE INDEX idx_chunks_book ON chunks(book_id);
CREATE INDEX idx_chunks_dup ON chunks(dup_of);
CREATE INDEX idx_toc_book ON toc(book_id);
"""

SCHEMA = "DROP TABLE IF EXISTS chunks, paras, toc, docs, books CASCADE;\n" + SCHEMA_BODY


def build() -> None:
    t0 = time.time()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(**settings.dsn)
    # claim_evidence.chunk_id 外键引用 chunks（共识层）：重建 chunks 时先摘掉外键再挂回，
    # chunk_id 生成是确定性的（同语料重解析 id 不变），重建后引用仍然有效
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('claim_evidence')")
        has_claim_evidence = cur.fetchone()[0] is not None
        if has_claim_evidence:
            cur.execute("ALTER TABLE claim_evidence DROP CONSTRAINT IF EXISTS claim_evidence_chunk_id_fkey")
            cur.execute("DROP TABLE IF EXISTS chunks, paras, toc, docs, books CASCADE")
            cur.execute(SCHEMA_BODY)
            cur.execute(
                "ALTER TABLE claim_evidence ADD CONSTRAINT claim_evidence_chunk_id_fkey"
                " FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE"
            )
        else:
            cur.execute(SCHEMA)
    conn.commit()
    conn.close()

    alloc = _IdAlloc()
    jwly = parse_epub(settings.epub_jwly, "jwly", "戒为良药", "飞翔", alloc)
    jzl = parse_epub(settings.epub_jzl, "jzl", "戒者录", "前辈", alloc)
    books = [jwly, jzl]
    print(f"[1/5] 解析完成：{jwly.title} {jwly.doc_count}页/{jwly.char_count:,}字，"
          f"{jzl.title} {jzl.doc_count}页/{jzl.char_count:,}字，目录节点 {len(jwly.toc)}+{len(jzl.toc)}")

    chunks: list[Chunk] = []
    for book in books:
        chunks.extend(chunk_book(book, id_start=len(chunks) + 1))
    print(f"[2/5] 分块完成：{len(chunks)} chunks "
          f"(均值 {sum(len(c.text) for c in chunks) // max(1, len(chunks))} 字)")

    marked = dedup(chunks)
    indexed = [c for c in chunks if not c.dup_of]
    print(f"[3/5] 去重完成：标记重复 {marked}，进入索引 {len(indexed)}")

    for c in chunks:
        # 题文联合索引：章节/小节标题并入 token 流，保证"只出现在标题里的主题词"可命中
        c.tokens = tokenize(c.text) + tokenize(
            f"{c.chapter_title} {c.section_title or ''}"
        )
    print(f"[4/5] 分词完成（jieba + 自定义词典）")

    # 实体打标挂钩（功能二）：新构建的 chunk 同步写 entities（terms 表已存在时）
    _tag_entities(chunks)

    db_desc = f"PostgreSQL {settings.pg_host}:{settings.pg_port}/{settings.pg_db}"
    conn = psycopg2.connect(**settings.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)

            cur.executemany(
                "INSERT INTO books VALUES(%s,%s,%s,%s,%s)",
                [(b.id, b.title, b.author, b.doc_count, b.char_count) for b in books],
            )
            for b in books:
                cur.executemany(
                    "INSERT INTO docs VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(d.id, b.id, d.seq, d.file, d.title, d.author,
                      sum(len(p.text) for p in d.paras), len(d.paras)) for d in b.docs],
                )
                cur.executemany(
                    "INSERT INTO paras VALUES(%s,%s,%s,%s,%s,%s)",
                    [(p.id, p.doc_id, p.seq, p.kind, p.text, p.elem_id) for d in b.docs for p in d.paras],
                )
                cur.executemany(
                    "INSERT INTO toc VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    [(e.id, b.id, e.doc_id, e.para_id, e.title, e.level, e.seq) for e in b.toc],
                )
            cur.executemany(
                "INSERT INTO chunks VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(c.id, c.book_id, c.doc_id, c.seq, json.dumps(c.para_ids, ensure_ascii=False),
                  c.breadcrumb, c.chapter_title, c.section_title, c.author, c.doc_type,
                  c.text, json.dumps(c.tokens, ensure_ascii=False), c.dup_of,
                  json.dumps(c.entities, ensure_ascii=False)) for c in chunks],
            )
        conn.commit()
        print(f"[5/5] {db_desc} 写入完成")
        conn.close()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    by_type: dict[str, int] = {}
    for c in indexed:
        by_type[c.doc_type] = by_type.get(c.doc_type, 0) + 1
    print(f"      doc_type 分布: {by_type}")

    # 可选：向量索引
    if settings.emb_key and settings.embedding_model and settings.emb_base:
        _build_embeddings(indexed)
    else:
        print("[skip] 未配置 EMBEDDING_*，跳过向量索引（BM25-only 模式）")

    print(f"构建总耗时 {time.time() - t0:.1f}s")


def _tag_entities(chunks) -> None:
    """实体打标挂钩（功能二）：terms 表已落库时，text + 章节标题 → chunks.entities。

    失败不阻断构建（打标也可事后用 scripts/backfill_entities.py 补齐）。
    """
    try:
        from app.db import conn_ctx, init_pool
        from app.entities import match_entities_typed

        init_pool()
        with conn_ctx() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM terms WHERE enabled")
            n = cur.fetchone()[0]
        if not n:
            print("[skip] terms 表为空，跳过实体打标（先运行 scripts/seed_terms.py）")
            return
        for c in chunks:
            c.entities = match_entities_typed(
                f"{c.chapter_title}\n{c.section_title or ''}\n{c.text}"
            )
        print(f"[4.5/5] 实体打标完成（{n} 术语）")
    except Exception as e:  # noqa: BLE001
        print(f"[skip] 实体打标失败（不影响构建，可后补 backfill_entities.py）: {e}")


def _build_embeddings(chunks: list[Chunk]) -> None:
    import numpy as np
    from openai import OpenAI

    print(f"[emb] 开始生成向量索引：{len(chunks)} chunks, model={settings.embedding_model}")
    client = OpenAI(base_url=settings.emb_base, api_key=settings.emb_key, timeout=60)
    batch = 32
    vectors: list[list[float]] = []
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        for attempt in range(3):
            try:
                resp = client.embeddings.create(
                    model=settings.embedding_model,
                    input=[(" ".join(c.tokens) or c.text) for c in part],
                )
                vectors.extend(d.embedding for d in resp.data)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"[emb] 第 {i}-{i + len(part)} 批次失败，放弃向量索引: {e}")
                    return
                time.sleep(2 ** attempt)
        if i % (batch * 10) == 0:
            print(f"[emb] {i}/{len(chunks)}")
    matrix = np.array(vectors, dtype=np.float32)
    np.save(settings.emb_matrix_path, matrix)
    with open(settings.emb_ids_path, "w", encoding="utf-8") as f:
        json.dump([c.id for c in chunks], f, ensure_ascii=False)
    print(f"[emb] 向量索引完成 → {settings.emb_matrix_path} shape={matrix.shape}")


if __name__ == "__main__":
    build()

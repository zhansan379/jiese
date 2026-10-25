"""混合检索：BM25 ⊕ 可选向量 → RRF 融合 → 引用列表。含一次规则触发的无扩展降级重试。"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import numpy as np

from .config import settings
from .glossary import expand_tokens, tokenize
from .indexer import BM25

RRF_K = 60


@dataclass
class Citation:
    chunk_id: str
    book_id: str
    book_title: str
    chapter_title: str
    breadcrumb: str
    author: str | None
    doc_type: str
    doc_id: str
    para_ids: list[str]
    text: str
    score: float

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id, "book_id": self.book_id,
            "book_title": self.book_title, "chapter_title": self.chapter_title,
            "breadcrumb": self.breadcrumb, "author": self.author,
            "doc_type": self.doc_type, "doc_id": self.doc_id,
            "para_ids": self.para_ids, "text": self.text, "score": round(self.score, 4),
        }


class Retriever:
    def __init__(self, conn):
        with conn.cursor() as cur:
            cur.execute("SELECT id, tokens FROM chunks WHERE dup_of IS NULL ORDER BY seq")
            rows = cur.fetchall()
            ids = [r[0] for r in rows]
            token_lists = [json.loads(r[1]) for r in rows]
            self.bm25 = BM25(ids, token_lists)
            cur.execute(
                "SELECT id, book_id, breadcrumb, chapter_title, author, doc_type, doc_id, para_ids, text "
                "FROM chunks WHERE dup_of IS NULL"
            )
            self.chunk_meta = {r[0]: r for r in cur.fetchall()}
            cur.execute("SELECT id, title FROM books")
            self.book_titles = {r[0]: r[1] for r in cur.fetchall()}
        self._load_vectors(conn)
        self._load_vector_books()

    def _load_vectors(self, conn) -> None:
        """向量从 PostgreSQL(pgvector) 加载到内存矩阵（启动一次，查询期零 SQL）。"""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, embedding::text FROM chunks "
                    "WHERE dup_of IS NULL AND embedding IS NOT NULL ORDER BY seq"
                )
                rows = cur.fetchall()
            if not rows:
                self.vectors, self.vector_ids = None, []
                return
            self.vector_ids = [r[0] for r in rows]
            mat = np.array(
                [np.fromstring(r[1].strip("[]"), sep=",") for r in rows], dtype=np.float32
            )
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self.vectors = mat / norms
        except Exception as e:  # noqa: BLE001
            print(f"[retriever] pgvector 加载失败，回退 BM25-only: {e}")
            self.vectors, self.vector_ids = None, []

    def _load_vector_books(self) -> None:
        """向量行 → book_id 对齐数组（按书过滤用 mask）。"""
        self.vector_books = None
        if self.vectors is None:
            return
        self.vector_books = np.array(
            [self.chunk_meta[cid][1] if cid in self.chunk_meta else "" for cid in self.vector_ids]
        )

    @property
    def vector_ready(self) -> bool:
        return self.vectors is not None and len(self.vector_ids) > 0

    # ---- 重排序：RRF 排名与 rerank 排名加权融合（评测选定 bge-reranker-v2-m3, w=0.5）----
    def _rerank_fuse(
        self, query: str, merged: list[tuple[str, float]], limit: int = 24
    ) -> list[tuple[str, float]]:
        head, tail = merged[:limit], merged[limit:]
        cids = [cid for cid, _ in head]
        docs = [f"{self.chunk_meta[cid][3]} {self.chunk_meta[cid][8]}" for cid in cids]
        try:
            resp = httpx.post(
                f"{settings.openai_base_url.rstrip('/')}/rerank",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": settings.rerank_model, "query": query,
                      "documents": docs, "top_n": len(docs)},
                timeout=30,
            )
            results = resp.json()["results"]
            rr_rank = {cids[x["index"]]: n for n, x in enumerate(results)}
        except Exception as e:  # noqa: BLE001
            print(f"[retriever] rerank 失败，保持 RRF 序: {e}")
            return merged
        w = settings.rerank_weight
        fused = [(cid, s + w * 1.0 / (60 + rr_rank.get(cid, 99) + 1)) for cid, s in head]
        fused.sort(key=lambda x: -x[1])
        # 绝对相关性（评测：阈值0.1 相关集零损失、离题集 6→0.9 条）
        self.last_rerank_scores = {
            cids[x["index"]]: x["relevance_score"] for x in results
        }
        return fused + tail

    # ---- 向量检索（查询时调 embedding API，内存矩阵点积；可按书 mask）----
    def _vector_search(self, query: str, top_k: int, book_id: str | None = None) -> list[tuple[str, float]]:
        if not self.vector_ready:
            return []
        try:
            from openai import OpenAI
            client = OpenAI(base_url=settings.emb_base, api_key=settings.emb_key, timeout=30)
            text = " ".join(expand_tokens(tokenize(query)))
            resp = client.embeddings.create(model=settings.embedding_model, input=[text])
            q = np.array(resp.data[0].embedding, dtype=np.float32)
            q = q / (np.linalg.norm(q) or 1.0)
            sims = self.vectors @ q
            if book_id and self.vector_books is not None:
                sims = np.where(self.vector_books == book_id, sims, -1.0)
            order = np.argsort(-sims)[:top_k]
            return [(self.vector_ids[i], float(sims[i])) for i in order]
        except Exception as e:  # noqa: BLE001
            print(f"[retriever] 查询向量获取失败（本次仅用 BM25）: {e}")
            return []

    def retrieve(
        self, query: str, k: int | None = None, smart_search: bool = False,
        book_id: str | None = None,
    ) -> list[Citation]:
        k = k or (settings.top_k + 2 if smart_search else settings.top_k)
        cand_k = settings.candidate_k * 2 if smart_search else settings.candidate_k

        tokens = tokenize(query)
        expanded = expand_tokens(tokens)
        bm25_hits = self.bm25.search(expanded, cand_k * 3 if book_id else cand_k)

        if not bm25_hits:  # 有界兜底：无扩展降级重试一次
            bm25_hits = self.bm25.search(tokens, cand_k * 3 if book_id else cand_k)

        if book_id:  # 按书过滤（BM25 后滤）
            bm25_hits = [(cid, s) for cid, s in bm25_hits
                         if cid in self.chunk_meta and self.chunk_meta[cid][1] == book_id][:cand_k]

        vec_hits = self._vector_search(query, cand_k, book_id=book_id) if self.vector_ready else []

        # RRF 融合
        rrf: dict[str, float] = {}
        for hits in (bm25_hits, vec_hits):
            for rank, (cid, _) in enumerate(hits):
                rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)

        merged = sorted(rrf.items(), key=lambda kv: -kv[1])
        self.last_rerank_scores = {}
        if settings.rerank_enabled:
            merged = self._rerank_fuse(query, merged)
        if smart_search:
            # 同一章最多 3 条，避免单章刷屏，保证多主题覆盖
            per_chapter: dict[str, int] = {}
            diversified = []
            for cid, score in merged:
                row = self.chunk_meta.get(cid)
                if not row:
                    continue
                chapter = row[3]
                if per_chapter.get(chapter, 0) >= 3:
                    continue
                per_chapter[chapter] = per_chapter.get(chapter, 0) + 1
                diversified.append((cid, score))
            merged = diversified
        if settings.rerank_enabled and settings.rerank_threshold > 0:
            merged = [x for x in merged if self.last_rerank_scores.get(x[0], 0) >= settings.rerank_threshold]
        merged = merged[:k]
        out = []
        for cid, score in merged:
            row = self.chunk_meta.get(cid)
            if not row:
                continue
            _, book_id, breadcrumb, chapter_title, author, doc_type, doc_id, para_ids_json, text = row
            out.append(Citation(
                chunk_id=cid, book_id=book_id,
                book_title=self.book_titles.get(book_id, ""),
                chapter_title=chapter_title, breadcrumb=breadcrumb,
                author=author, doc_type=doc_type, doc_id=doc_id,
                para_ids=json.loads(para_ids_json), text=text, score=score,
            ))
        return out

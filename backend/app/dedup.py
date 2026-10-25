"""文本级去重：精确（规范化 MD5）+ 近似（8-gram shingle Jaccard）。

- 只标记 dup_of，不物理删除；重复 chunk 不进检索索引，阅读器不受影响
- 规范版本优先级：先处理的书（戒为良药=原创）> 后处理的书（戒者录=转载），同书内靠前章节优先
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .chunker import Chunk

_NEAR_DUP_JACCARD = 0.85
_SHINGLE_K = 8
_MAX_POSTING = 30   # 跳过过于常见的 shingle

_PUNCT = re.compile(r"[，。、；：！？…—·\-\s,.:;!?()（）【】《》「」『』“”\"'‘’·*#]+")


def normalize(text: str) -> str:
    return _PUNCT.sub("", text)


def dedup(chunks: list[Chunk]) -> int:
    """就地将重复 chunk 标记 dup_of，返回标记数量。chunks 顺序即优先级（靠前为规范版本）。"""
    marked = 0
    norm2canonical: dict[str, str] = {}

    for c in chunks:
        if c.dup_of:
            continue
        key = hashlib.md5(normalize(c.text).encode("utf-8")).hexdigest()
        if key in norm2canonical:
            c.dup_of = norm2canonical[key]
            marked += 1
        else:
            norm2canonical[key] = c.id

    # 近似去重：仅在"未命中精确去重"的 chunk 间进行
    pending = [c for c in chunks if not c.dup_of]
    shingles: dict[str, set[int]] = {}
    chunk_sh: list[set[int]] = []
    for i, c in enumerate(pending):
        norm = normalize(c.text)
        sh = {hash(norm[j:j + _SHINGLE_K]) for j in range(max(0, len(norm) - _SHINGLE_K + 1))}
        chunk_sh.append(sh)
        for s in sh:
            shingles.setdefault(s, []).append(i)

    # 倒排索引找候选对（跳过高频 shingle）
    pair_count: dict[tuple[int, int], int] = defaultdict(int)
    for s, postings in shingles.items():
        if len(postings) > _MAX_POSTING:
            continue
        for a in range(len(postings)):
            for b in range(a + 1, len(postings)):
                i, j = postings[a], postings[b]
                if i > j:
                    i, j = j, i
                pair_count[(i, j)] += 1

    for (i, j), _ in sorted(pair_count.items(), key=lambda kv: -kv[1]):
        ci, cj = pending[i], pending[j]
        if ci.dup_of or cj.dup_of:
            continue
        union = len(chunk_sh[i] | chunk_sh[j])
        if union == 0:
            continue
        jac = len(chunk_sh[i] & chunk_sh[j]) / union
        if jac >= _NEAR_DUP_JACCARD:
            # i<j 与 pending 顺序一致，pending 顺序即规范优先级
            cj.dup_of = ci.id
            marked += 1

    return marked

"""实体标注 + 受限关系（实现计划功能二：docs/实现计划-共识层-实体-画像.md）。

- 术语表存 PostgreSQL（terms 表，类型：症状/方法/因素/概念；aliases 同义词归一到规范词），
  管理后台「术语与关系」Tab 可增删改。
- 匹配：terms + aliases 展开为模式字典，Aho-Corasick 风格首字符索引的多模式匹配
  （纯 dict 实现）：扫描文本每个位置，仅尝试以该字符开头的模式。
- chunk 标注：构建期 scripts/backfill_entities.py / build_kb.py 挂钩写 chunks.entities
  （形如 {"症状":["频遗"],"方法":["固肾功"]}）；运行期 Retriever 启动加载 chunk_id → set(terms)。
- 关系（term_edges）：方法—对治→问题、因素—导致→症状，由 scripts/mine_edges.py 模式匹配挖掘，
  evidence 记录来源 chunk_ids；运行时只使用 evidence ≥ 2 的边（低支持度为噪声）。
- 查询实体识别：retrieve() 内对 query 做同字典匹配 → query_entities；
  候选 chunk 实体与查询实体有交集 → RRF 分数 ×1.2（加成而非硬过滤，交集为空不加成）。
"""
from __future__ import annotations

import json
import time

from .db import conn_ctx

TYPES = ("症状", "方法", "因素", "概念")
REL_TREAT = "对治"
REL_CAUSE = "导致"

# 运行时边支持度门槛：evidence chunk 数低于该值的关系边不进运行时（噪声）
MIN_EDGE_SUPPORT = 2

# 候选加成系数：候选 chunk 实体与查询实体有交集 → RRF 分数 ×ENTITY_BOOST
ENTITY_BOOST = 1.2

# 方法清单注入生成的上限（每个问题实体最多列出的方法数 / 最多覆盖的问题实体数）
MAX_METHODS_PER_HINT = 6
MAX_HINT_ENTITIES = 3

_CACHE: dict | None = None       # {"matcher": _Matcher, "types": {term: type}, "terms": [...]}
_EDGES: list[dict] | None = None  # 运行时边（evidence ≥ MIN_EDGE_SUPPORT，仅 对治 进生成段）
_CACHE_AT = 0.0
_CACHE_TTL = 60.0


class _Matcher:
    """多模式匹配器：模式按首字符分组，扫描文本逐位置尝试（Aho-Corasick 风格的 dict 简化版）。"""

    def __init__(self, patterns: dict[str, str]):
        # patterns: 匹配串（小写化）→ 规范术语
        self.by_first: dict[str, list[tuple[str, str]]] = {}
        for pat, term in patterns.items():
            if pat:
                self.by_first.setdefault(pat[0], []).append((pat, term))
        # 同首字符长模式优先，保证「前列腺炎」这类长词优先命中规范词
        for cands in self.by_first.values():
            cands.sort(key=lambda x: -len(x[0]))

    def match(self, text: str) -> set[str]:
        text = (text or "").lower()
        hits: set[str] = set()
        for i, ch in enumerate(text):
            cands = self.by_first.get(ch)
            if not cands:
                continue
            for pat, term in cands:
                if term in hits:
                    continue
                if text.startswith(pat, i):
                    hits.add(term)
        return hits


def refresh() -> None:
    """加载启用的术语（含别名展开）与运行时关系边，重建匹配器缓存。"""
    global _CACHE, _EDGES, _CACHE_AT
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT term, type, aliases FROM terms WHERE enabled")
        rows = cur.fetchall()
        cur.execute(
            "SELECT id, src_term, rel, dst_term, evidence FROM term_edges"
            " WHERE jsonb_array_length(evidence) >= %s",
            (MIN_EDGE_SUPPORT,),
        )
        edge_rows = cur.fetchall()
    patterns: dict[str, str] = {}
    types: dict[str, str] = {}
    terms: list[dict] = []
    for term, ttype, aliases in rows:
        if not term:
            continue
        types[term] = ttype
        terms.append({"term": term, "type": ttype,
                      "aliases": aliases if isinstance(aliases, list) else []})
        patterns.setdefault(term.lower(), term)
        for a in (aliases if isinstance(aliases, list) else []):
            if isinstance(a, str) and a.strip():
                patterns.setdefault(a.strip().lower(), term)
    _CACHE = {"matcher": _Matcher(patterns), "types": types, "terms": terms}
    # 运行时边：仅保留「对治」进生成段（「导致」边管理端可查，暂不注入生成）
    _EDGES = [
        {"id": r[0], "src_term": r[1], "rel": r[2], "dst_term": r[3],
         "evidence": r[4] if isinstance(r[4], list) else []}
        for r in edge_rows if r[2] == REL_TREAT
    ]
    _CACHE_AT = time.monotonic()


def _load() -> dict:
    global _CACHE, _CACHE_AT
    if _CACHE is None or time.monotonic() - _CACHE_AT > _CACHE_TTL:
        try:
            refresh()
        except Exception as e:  # noqa: BLE001  数据库暂不可用时沿用旧缓存
            print(f"[entities] 术语缓存加载失败，沿用旧缓存: {e}")
            _CACHE = _CACHE or {"matcher": _Matcher({}), "types": {}, "terms": []}
            _CACHE_AT = time.monotonic()
    return _CACHE


def _edges() -> list[dict]:
    global _EDGES
    _load()
    if _EDGES is None:
        try:
            refresh()
        except Exception:  # noqa: BLE001
            _EDGES = []
    return _EDGES


def match_entities(text: str) -> set[str]:
    """文本 → 命中的规范术语集合（terms + aliases，大小写不敏感）。"""
    return _load()["matcher"].match(text)


def match_entities_typed(text: str) -> dict[str, list[str]]:
    """文本 → {"症状": [...], "方法": [...]}：按类型分组的命中术语（打标注入 chunks.entities）。"""
    types = _load()["types"]
    out: dict[str, list[str]] = {}
    for term in match_entities(text):
        out.setdefault(types.get(term, "概念"), []).append(term)
    return out


def load_chunk_entities(conn) -> dict[str, set[str]]:
    """启动时加载 chunk_id → set(terms)（Retriever 内存 dict，6434 条几百 KB）。"""
    mapping: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, entities FROM chunks WHERE dup_of IS NULL AND entities::text != '{}'")
        for cid, ents in cur.fetchall():
            if isinstance(ents, str):
                try:
                    ents = json.loads(ents)
                except ValueError:
                    continue
            if isinstance(ents, dict):
                terms = {t for lst in ents.values() for t in (lst or [])}
                if terms:
                    mapping[cid] = terms
    return mapping


def collect_method_hints(query_entities: set[str], citations) -> str:
    """查询实体命中的「对治」边 → 生成上下文方法清单段。

    只列证据 chunk 落在当前引用中的边（保证方法可被 [n] 引用支撑，符合"仅依据摘录回答"约束）：
        书中对治频遗的方法清单：固肾功、睡前拉筋（来源 [2][5]）
    无命中返回空串。
    """
    if not query_entities:
        return ""
    cite_index = {c.chunk_id: i for i, c in enumerate(citations, 1)}
    by_problem: dict[str, list[dict]] = {}
    for edge in _edges():
        if edge["dst_term"] in query_entities:
            by_problem.setdefault(edge["dst_term"], []).append(edge)
    if not by_problem:
        return ""
    lines = []
    for problem, edges in sorted(by_problem.items(), key=lambda kv: -len(kv[1]))[:MAX_HINT_ENTITIES]:
        methods: list[str] = []
        sources: list[int] = []
        for edge in sorted(edges, key=lambda e: -len(e["evidence"])):
            if len(methods) >= MAX_METHODS_PER_HINT:
                break
            if edge["src_term"] not in methods:
                methods.append(edge["src_term"])
            for cid in edge["evidence"]:
                idx = cite_index.get(cid)
                if idx and idx not in sources:
                    sources.append(idx)
        if methods:
            src = f"（来源 {''.join(f'[{s}]' for s in sources[:4])}）" if sources else ""
            lines.append(f"书中对治{problem}的方法清单：{'、'.join(methods)}{src}")
    if not lines:
        return ""
    return "书中方法清单（可信度参考）：\n" + "\n".join(f"- {l}" for l in lines)

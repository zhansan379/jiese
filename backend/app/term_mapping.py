"""查询词映射 / 术语归一化（参考 ragent QueryTermMappingService / QueryTermMappingUtil）。

- 规则存 PostgreSQL（query_term_mappings 表），管理后台可增删改查
- 检索链路先把口语/缩写/别名归一化为标准术语（如 "yy" → "意淫"），提高召回
- 匹配类型：1 精确（子串替换） 2 前缀 3 正则 4 整词；priority 数值越小越先应用，
  同优先级长源词在前，避免短词抢先替换破坏长词
- 进程内缓存：规则增删改即失效重建（对齐 ragent 的 Redis 缓存 + CRUD 清缓存）
"""
from __future__ import annotations

import re
import time

from .db import conn_ctx

MATCH_EXACT = 1    # 精确：子串替换
MATCH_PREFIX = 2   # 前缀：句首或标点/空白之后
MATCH_REGEX = 3    # 正则：source_term 直接作为正则模式
MATCH_WORD = 4     # 整词：ASCII 词不受更长词/汉字包裹

_CACHE: list[dict] | None = None
_CACHE_AT = 0.0
_CACHE_TTL = 60.0  # 秒；兜底刷新（正常路径由管理端 CRUD 主动 refresh）

# 前缀匹配允许的起始边界：文本开头或这些分隔符之后
_RE_PREFIX_SEP = r"[\s，。？！；：、,.!?;:()（）\[\]【】\"'“”‘’…—~]"
# 整词匹配：前后不能是字母/数字/下划线/汉字（拉丁缩写不粘字）


def refresh() -> None:
    """失效并重建缓存（管理端增删改后调用，对齐 ragent clearCache）。"""
    global _CACHE, _CACHE_AT
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, domain, source_term, target_term, match_type, priority"
            " FROM query_term_mappings WHERE enabled"
            " ORDER BY priority, length(source_term) DESC, id"
        )
        _CACHE = [dict(r) for r in cur.fetchall()]
    _CACHE_AT = time.monotonic()


def _load() -> list[dict]:
    global _CACHE, _CACHE_AT
    if _CACHE is None or time.monotonic() - _CACHE_AT > _CACHE_TTL:
        try:
            refresh()
        except Exception as e:  # noqa: BLE001  数据库暂不可用时沿用旧缓存
            print(f"[term-mapping] 规则加载失败，沿用旧缓存: {e}")
            _CACHE = _CACHE or []
            _CACHE_AT = time.monotonic()
    return _CACHE


def _apply_exact(text: str, source: str, target: str) -> str:
    """精确替换：移植 ragent QueryTermMappingUtil.applyMapping。

    命中位置若已是目标词开头（如文本里本来就该是"平安保司"），按原文跳过，防止重复替换。
    扫描始终基于原文、结果写进独立缓冲，替换产物不会被再次扫描，无死循环。
    """
    sb: list[str] = []
    idx = 0
    src_len, tgt_len = len(source), len(target)
    while idx < len(text):
        hit = text.find(source, idx)
        if hit < 0:
            sb.append(text[idx:])
            break
        sb.append(text[idx:hit])
        if text.startswith(target, hit):
            sb.append(text[hit:hit + tgt_len])
            idx = hit + tgt_len
        else:
            sb.append(target)
            idx = hit + src_len
    return "".join(sb)


def _apply_rule(text: str, source: str, target: str, match_type: int) -> str:
    if match_type == MATCH_EXACT:
        return _apply_exact(text, source, target)
    if match_type == MATCH_REGEX:
        # 一次 re.sub 不会重扫替换产物；空匹配保护避免零宽模式死循环
        return re.sub(source, target.replace("\\", "\\\\"), text) if source else text
    if match_type == MATCH_PREFIX:
        pattern = rf"(?:^|(?<={_RE_PREFIX_SEP})){re.escape(source)}"
        return re.sub(pattern, target.replace("\\", "\\\\"), text)
    # 整词：仅对 ASCII 源词有区别（汉字之间没有词边界，退化为精确替换）。
    # 边界用 ASCII 词字符而非 \w：Python 的 \w 会匹配汉字，导致中文后面跟缩写（"咋整yy"）被误判为"非整词"
    if source.isascii():
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])"
        return re.sub(pattern, target.replace("\\", "\\\\"), text)
    return _apply_exact(text, source, target)


def normalize_with_hits(text: str) -> tuple[str, list[dict]]:
    """归一化并返回应用的规则清单（供链路追踪节点 extra_data 记录）。"""
    if not text:
        return text, []
    result = text
    hits: list[dict] = []
    for m in _load():
        source, target = m["source_term"], m["target_term"]
        match_type = m["match_type"]
        if not source or not target or source == target:
            continue
        if match_type == MATCH_REGEX:
            try:
                re.compile(source)
            except re.error:
                continue
        applied = _apply_rule(result, source, target, match_type)
        if applied != result:
            hits.append({"source": source, "target": target, "match_type": match_type})
            result = applied
    return result, hits


def normalize(text: str) -> str:
    """对用户问题做术语归一化（与 ragent QueryTermMappingService.normalize 对齐的入口）。"""
    return normalize_with_hits(text)[0]

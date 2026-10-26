"""术语同义词治理（实现计划功能二的数据质量层）。

问题：术语表里存在「同一个词既是 A 的别名、又是独立术语 B」的冲突
（如 SY / 手淫 / 撸管 三份、焦虑 / 焦虑症、抑郁 / 抑郁症、强迫 / 强迫症、
神衰 / 神经症、觉知 / 觉察、看黄 / 搜黄、淫梦 / 春梦 …），以及语料里常见
但未收录的口语表述（自慰、打飞机、淫念、黄图、黄源、秃顶、不举 …）。

治理规则：
- MERGES：把重复术语合并到规范词（被合并词及其别名并入规范词的别名，然后删除该词条）
- ALIAS_REMOVE：保留独立术语、从别名里剔除（语义不重合或会造成子串误匹配）
- ALIAS_ADD：从语料扫描补充的口语同义词
- MAPPING_BLOCKLIST：可作实体别名（仅影响识别），但**不写入查询词映射**
  （改写 query 文本会改变语义，如「复发」既指破戒复发也指症状复发）

seed_terms.py 与 scripts/normalize_terms.py 共用本模块，保证生成/迁移一致。
"""
from __future__ import annotations

# 被合并词 → 规范词（顺序无关；合并后别名并入规范词）
MERGES: dict[str, str] = {
    "SY": "手淫",
    "撸管": "手淫",
    "恶习": "手淫",
    "YY": "意淫",
    "邪念": "意淫",
    "春梦": "淫梦",
    "搜黄": "看黄",
    "色情": "看黄",
    "焦虑": "焦虑症",
    "抑郁": "抑郁症",
    "强迫": "强迫症",
    "神衰": "神经症",
    "觉知": "觉察",
}

# 从这些规范词的别名里剔除（保留对应独立术语，或语义不重合）
ALIAS_REMOVE: dict[str, tuple[str, ...]] = {
    "看黄": ("擦边",),            # 擦边（擦边内容）是独立触发因素
    "觉悟": ("觉察", "觉知"),      # 觉察/觉知是方法，不是「觉悟」的同义词
    "早泄": ("性能力",),           # 性能力属于「性功能」概念
    "手淫": ("撸",),               # 单字别名 ⊂ 撸管，易误匹配
    "脑力": ("注意力",),           # 注意力 ⊂ 转移注意力（方法），会误标
    "失眠": ("睡眠",),             # 睡眠 ≠ 失眠（「改善睡眠」不能改写成「改善失眠」）
}

# 语料扫描补充的口语同义词（term → 新别名）
ALIAS_ADD: dict[str, tuple[str, ...]] = {
    "手淫": ("自慰", "打飞机", "撸一发", "自摸", "手撸"),
    "意淫": ("淫念", "意恶"),
    "遗精": ("遗泄", "漏精", "漏丹"),
    "看黄": ("黄图", "黄源", "看片"),
    "脱发": ("秃顶",),
    "阳痿": ("不举",),
    "断念口诀": ("念起即断", "觉之即无"),
    "固肾功": ("双手攀足",),
    "无害论": ("适度无害",),
    "禁欲有害论": ("禁欲有害",),
    "阴囊潮湿": ("湿乎乎", "湿漉漉"),
}

# 只作实体别名、不写入查询词映射的别名（改写 query 会改变语义）
MAPPING_BLOCKLIST = frozenset({"复发", "复破", "邪淫", "撸", "睡眠", "注意力"})


def normalize_alias_map(alias_map: dict[str, list[str]]) -> tuple[dict[str, list[str]], list[str]]:
    """对 term → aliases 映射做合并/清理/补充。

    返回 (规范化后的映射, 被合并删除的词条列表)。幂等：重复调用结果不变。
    """
    out: dict[str, list[str]] = {t: list(a or []) for t, a in alias_map.items()}
    merged: list[str] = []
    for src, dst in MERGES.items():
        if src in out and dst in out and src != dst:
            for a in [src, *out[src]]:
                a = (a or "").strip()
                if a and a != dst and a not in out[dst]:
                    out[dst].append(a)
            del out[src]
            merged.append(src)

    canonical = set(out)
    cleaned: dict[str, list[str]] = {}
    for term, aliases in out.items():
        kept: list[str] = []
        for a in aliases:
            a = (a or "").strip()
            if not a or a == term or a in kept:
                continue
            if a in canonical:                     # 别名是另一个独立术语 → 冲突
                continue
            if a in ALIAS_REMOVE.get(term, ()):
                continue
            kept.append(a)
        for a in ALIAS_ADD.get(term, ()):
            if a not in kept and a != term and a not in canonical:
                kept.append(a)
        cleaned[term] = kept
    return cleaned, merged


def mapping_candidates(terms: dict[str, list[str]]) -> tuple[list[tuple[str, str, int]], list[tuple[str, str, str]]]:
    """从术语别名生成查询词映射候选。

    返回 (可写入的 [(source, target, match_type)], 被跳过的 [(source, target, 原因)])。
    过滤规则：
    - 单字/单字母（子串误伤高发，如「撸」⊂「撸管」「AV」⊂「have」）
    - 别名等于任一独立术语（术语本身不需要被改写）
    - 别名是任一独立术语的子串（会破坏长词，如「口诀」⊂「断念口诀」→「断念念口诀」）
    - 别名是目标词自身的子串（如「前列腺」⊂「前列腺炎」→「前列腺炎炎」）
    - MAPPING_BLOCKLIST（可做别名但改写 query 会改变语义）
    """
    canonical = set(terms)
    ok: list[tuple[str, str, int]] = []
    skipped: list[tuple[str, str, str]] = []
    for term, aliases in terms.items():
        if term.isascii():
            for a in aliases:
                skipped.append((a, term, "目标词是缩写（改写后不如原词可读）"))
            continue
        for a in aliases:
            a = (a or "").strip()
            if not a:
                continue
            if len(a) < 2:
                skipped.append((a, term, "单字（子串误伤）"))
                continue
            if a in MAPPING_BLOCKLIST:
                skipped.append((a, term, "语义歧义（仅作实体别名）"))
                continue
            if a in canonical:
                skipped.append((a, term, "本身是独立术语"))
                continue
            if a in term:
                skipped.append((a, term, "是目标词子串（会破坏目标词）"))
                continue
            host = next((c for c in canonical if c != term and a in c), None)
            if host:
                skipped.append((a, term, f"是术语「{host}」的子串（会破坏长词）"))
                continue
            ok.append((a, term, 4 if a.isascii() else 1))
    return ok, skipped

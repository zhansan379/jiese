"""术语表：同义词族（查询扩展）+ jieba 自定义词典（保证领域词不被切碎）。"""
from __future__ import annotations

import re

# 同义词族：查询命中任一成员 → 整族并入检索
FAMILIES: list[list[str]] = [
    ["SY", "手淫", "撸管", "撸", "自渎", "恶习"],
    ["遗精", "梦遗", "滑精", "频遗", "失精"],
    ["意淫", "YY", "邪念", "邪淫"],
    ["神衰", "神经衰弱", "神经症"],
    ["社恐", "社交恐惧", "社交恐惧症"],
    ["痤疮", "青春痘", "痘痘"],
    ["脑力", "记忆力", "注意力"],
    ["早泄", "阳痿", "性功能", "性能力"],
    ["断念", "断念口诀", "念起即觉"],
    ["淫梦", "春梦", "梦中撸管", "黄梦"],
    ["看黄", "搜黄", "黄片", "色情", "擦边"],
    ["肾虚", "伤精", "伤肾"],
    ["失眠", "睡眠问题", "睡不着", "睡眠"],
    ["焦虑", "焦虑症"],
    ["强迫", "强迫症"],
    ["抑郁", "抑郁症"],
    ["觉悟", "觉察", "觉知"],
    ["破戒", "复破", "复发"],
    ["恢复", "康复", "痊愈"],
    ["前列腺炎", "前列腺"],
    ["脱发", "掉发", "掉头发"],
]

# jieba 自定义词典：领域词保证整体成词
CUSTOM_WORDS = [
    "戒色", "戒友", "心魔", "撸管", "断念", "断念口诀", "念起即觉", "觉之即无",
    "固肾功", "提肛", "观心", "念佛", "不净观", "白骨观", "觉知", "觉察", "觉悟",
    "欲望休眠期", "戒色厌倦期", "戒断反应", "无害论", "禁欲有害论", "伤精", "肾虚",
    "神衰", "社恐", "脑力", "频遗", "滑精", "遗精", "意淫", "破戒", "强戒", "修心",
    "飞翔经验", "戒色吧", "戒色文章", "养生", "早泄", "阳痿", "痤疮", "脱发",
    "前列腺炎", "精索静脉曲张", "神经症", "焦虑症", "强迫症", "抑郁症", "飞蚊症",
    "阴囊潮湿", "戒色德训", "看黄", "搜黄", "黄片", "擦边", "淫梦", "春梦",
    "梦中撸管", "破戒高峰期", "戒油子", "戒色稳定期", "觉悟等级",
]

_STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "们", "在", "和", "就", "都",
    "而", "与", "这", "那", "有", "人", "不", "也", "很", "到", "说", "要", "去",
    "会", "着", "没有", "看", "好", "自己", "什么", "怎么", "如何", "为什么", "还是",
    "一个", "一些", "这个", "那个", "这样", "那样", "但是", "如果", "因为", "所以",
    "可以", "可能", "已经", "应该", "觉得", "感觉", "时候", "现在", "就是", "还有",
    "我们", "很多", "让", "后", "对", "能", "做", "但", "多", "还", "开始", "知道",
    "真的", "变得", "一样", "请问", "一下", "大家", "老师", "求助", "谢谢",
}

# 索引噪声过滤：纯标点/符号、1-2 位纯数字（3 位以上如章号/天数/年份保留）、单英文字母
_RE_JUNK = re.compile(r"^[\W_]+$")
_RE_NUM12 = re.compile(r"^\d{1,2}$")
_RE_ASCII1 = re.compile(r"^[a-zA-Z]$")

_family_index: dict[str, list[str]] = {}
for fam in FAMILIES:
    for w in fam:
        _family_index.setdefault(w, fam)

_jieba_ready = False


def setup_jieba() -> None:
    global _jieba_ready
    if _jieba_ready:
        return
    import jieba
    for w in CUSTOM_WORDS:
        jieba.add_word(w)
    _jieba_ready = True


def tokenize(text: str) -> list[str]:
    setup_jieba()
    import jieba
    return [
        t for t in jieba.lcut(text)
        if t.strip() and t not in _STOPWORDS
        and not _RE_JUNK.match(t) and not _RE_NUM12.match(t) and not _RE_ASCII1.match(t)
    ]


def expand_tokens(tokens: list[str]) -> list[str]:
    """同义词族扩展：返回 tokens + 族内其他成员（去重、保序）。"""
    out = list(tokens)
    seen = set(tokens)
    for t in tokens:
        fam = _family_index.get(t)
        if fam:
            for w in fam:
                if w not in seen:
                    out.append(w)
                    seen.add(w)
    return out


def expand_text(text: str) -> str:
    """向量检索用的查询文本扩展。"""
    tokens = tokenize(text)
    return " ".join(expand_tokens(tokens))

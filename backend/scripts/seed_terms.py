"""术语表种子：三路来源合并 → CSV 草稿（人工校对可选）→ 导入 terms 表（实现计划功能二）。

来源：
1. app/glossary 的 FAMILIES（同义词族）与 CUSTOM_WORDS（jieba 自定义词典）
2. 《戒为良药》140+ 章标题挖掘（「X问题 / X破戒」模式 + 标题内直接命中术语）
3. chunks.tokens 的 TF-IDF 高权词（补充领域概念词）

类型（症状/方法/因素/概念）按领域知识预标注，允许人工校对：
    python scripts/seed_terms.py --gen terms.csv   # 生成 CSV 草稿（校对类型）
    python scripts/seed_terms.py --import terms.csv  # 导入（upsert）
    python scripts/seed_terms.py --auto             # 生成并直接导入（基线，免人工校对）
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import psycopg2

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.glossary import FAMILIES, CUSTOM_WORDS  # noqa: E402

# ---- 领域种子：显式标注（优先级最高，覆盖三路来源的类型） ----

SYMPTOMS = [
    "频遗", "遗精", "滑精", "早泄", "阳痿", "脱发", "痤疮", "社恐", "神衰", "神经症",
    "失眠", "焦虑症", "强迫症", "抑郁症", "前列腺炎", "精索静脉曲张", "飞蚊症", "阴囊潮湿",
    "肾虚", "伤精", "耳鸣", "腰痛", "腰酸", "白发", "黑眼圈", "眼袋", "痔疮", "鼻炎",
    "尿频", "尿分叉", "尿不尽", "尿泡沫", "精不液化", "血精", "睾丸下垂", "晨勃消失",
    "发育障碍", "视力下降", "多汗", "嗜睡", "口臭", "便秘", "结石", "气胸", "脾虚",
    "濒死感", "不真实感", "紧张性遗精", "恐惧症", "不孕不育", "脑力下降", "头晕", "头痛",
    "抽筋", "虚胖", "瘦弱", "出油", "憋精", "精索", "尿急",
]
METHODS = [
    "断念口诀", "断念", "观心", "觉察", "觉知", "不净观", "白骨观", "无常观", "固肾功",
    "提肛", "铁板桥", "拉筋", "艾灸", "泡脚", "散步", "慢跑", "有氧运动", "深蹲", "八段锦",
    "站桩", "养生桩", "打坐", "静坐", "冥想", "念佛", "持咒", "楞严咒", "食疗", "中药调理",
    "早睡", "养生", "情绪管理", "慎独", "学习戒色文章", "戒色日记", "日课", "戒色十规",
    "转移注意力", "深呼吸", "读经", "行善", "孝顺", "戒色文章",
]
FACTORS = [
    "看黄", "搜黄", "擦边", "意淫", "YY", "淫梦", "春梦", "熬夜", "久坐", "赖床", "独处",
    "手淫", "撸管", "SY", "婚前性行为", "酗酒", "吸烟", "暴食", "贪凉", "冷饮", "压力",
    "无聊", "情绪低落", "傲慢", "轻敌", "好奇",
]
CONCEPTS = [
    "心魔", "念头", "觉悟", "戒断反应", "欲望休眠期", "戒色厌倦期", "症状反复期", "翻种子",
    "破戒高峰期", "戒色稳定期", "戒油子", "无害论", "适度无害论", "禁欲有害论", "戒色动机",
    "警惕性", "德行", "元气", "肾气", "精气神", "阳气", "对境", "魔考", "怂恿", "觉悟等级",
    "恢复规律", "戒色意识", "修心", "强戒", "戒色黄金期", "断念实战", "念头实战", "晨勃",
    "精子质量", "吸收率", "性功能", "戒色觉悟", "戒色执行力", "破戒类型", "戒色状态",
]

# FAMILIES 规范词 → 类型（族内其余成员作别名；成员若同时是独立术语则单独建条）
FAMILY_TERM_TYPE = {
    "SY": "因素", "手淫": "因素", "遗精": "症状", "意淫": "因素", "神衰": "症状",
    "社恐": "症状", "痤疮": "症状", "脑力": "症状", "早泄": "症状", "断念": "方法",
    "淫梦": "因素", "看黄": "因素", "肾虚": "症状", "失眠": "症状", "焦虑": "症状",
    "强迫": "症状", "抑郁": "症状", "觉悟": "概念", "破戒": "概念", "恢复": "概念",
    "前列腺炎": "症状", "脱发": "症状",
}
# 族内成员需要独立成术语的（不并入族规范词的别名）
STANDALONE_FAMILY_MEMBERS = {"频遗", "神经症", "阳痿", "断念口诀", "觉知"}

# 章节标题挖掘的过滤模式
_RE_TITLE_PROBLEM = re.compile(r"([\u4e00-\u9fa5A-Za-z]{2,4}?)(?:问题|障碍)")
_RE_TITLE_FACTOR = re.compile(r"([\u4e00-\u9fa5A-Za-z]{2,4})破戒")
_TITLE_STOP = {"再谈", "浅谈", "直指", "关于", "详解", "详细", "深入", "补充", "案例", "研究",
               "分析", "破解", "论的", "如何", "怎样", "身体", "戒色", "恢复", "季节", "专题",
               "日常", "夏季", "春季", "秋季", "冬季", "关系", "习惯", "效率", "心态"}
_RE_TITLE_JUNK = re.compile(r"[的与和及了后之]")

# TF-IDF 高权词的通用词停用表（虚词/泛化词，非领域概念）
_TFIDF_STOP = {
    "一切", "一次", "一直", "一种", "一下", "一致", "不会", "不断", "不是", "不能", "不要",
    "不见", "严重", "之后", "之前", "人生", "他们", "她们", "以前", "以后", "其实", "内心",
    "出来", "出现", "别人", "发现", "后来", "最后", "容易", "导致", "造成", "必须", "快乐",
    "或者", "才能", "控制", "方法", "时间", "每天", "比较", "然后", "状态", "生活", "痛苦",
    "看到", "真正", "练习", "而且", "需要", "非常", "那么", "那种", "起来", "身体", "这种",
    "问题", "学习", "学会", "实战", "坚持", "成功", "情绪", "意识", "纯净", "能量", "欲望",
    "案例", "可以", "已经", "现在", "就是", "自己", "觉得", "感觉", "时候", "因为", "所以",
    "如果", "但是", "一个", "这个", "那个", "我们", "大家", "很多", "开始", "知道", "真的",
    "变得", "一样", "应该", "可能", "没有", "而是", "通过", "进行", "对于", "作为", "这些",
    "那些", "以及", "因此", "于是", "甚至", "根本", "完全", "绝对", "特别", "经常", "总是",
    "从来", "马上", "立刻", "突然", "忽然", "确实", "当然", "依然", "仍然", "只有", "只是",
    "还有", "还是", "就是", "如何", "怎样", "为什么", "什么", "怎么做", "什么样", "方面",
    "情况", "事情", "东西", "地方", "时候", "原因", "结果", "目的", "方式", "水平", "程度",
    "过程", "阶段", "效果", "作用", "影响", "危害", "好处", "坏处", "重要", "关键", "核心",
    "本身", "其中", "之间", "以上", "下面", "上面", "前面", "后面", "左右", "自己",
    "一天", "一年", "一点", "一般", "下降", "为了", "做到", "力量", "反复", "只要",
    "当时", "彻底", "心理", "总结", "慢慢", "提升", "文章", "无法", "注意", "父母",
    "笔记", "经历", "能够", "自然", "虽然", "表现", "认识", "越来越", "运动", "这位",
    "这是", "进入", "那时", "飞翔", "希望", "帮助", "失败", "思想", "行动", "结果",
}


def _base_terms() -> dict[str, dict]:
    """显式领域种子：term → {type, aliases}。"""
    terms: dict[str, dict] = {}

    def add(term: str, ttype: str, aliases: list[str] | None = None) -> None:
        term = term.strip()
        if not term or len(term) < 2:
            return
        e = terms.setdefault(term, {"type": ttype, "aliases": []})
        e["type"] = ttype
        for a in aliases or []:
            if a and a != term and a not in e["aliases"]:
                e["aliases"].append(a)

    for t in SYMPTOMS:
        add(t, "症状")
    for t in METHODS:
        add(t, "方法")
    for t in FACTORS:
        add(t, "因素")
    for t in CONCEPTS:
        add(t, "概念")

    # 同义词族：规范词 + 别名（显式种子优先，族仅补充别名与缺失词条）
    for fam in FAMILIES:
        canon = fam[0]
        aliases = [w for w in fam[1:] if w not in STANDALONE_FAMILY_MEMBERS]
        ttype = FAMILY_TERM_TYPE.get(canon, "概念")
        if canon in terms:
            for a in aliases:
                if a not in terms[canon]["aliases"] and a not in terms:
                    terms[canon]["aliases"].append(a)
        else:
            add(canon, ttype, aliases)
        # 族内独立成术语的成员
        for m in fam[1:]:
            if m in STANDALONE_FAMILY_MEMBERS and m not in terms:
                add(m, "症状" if m == "频遗" else FAMILY_TERM_TYPE.get(m, ttype))
    # 高频问题/因素的别名细化（频遗为对治关系的高频问题，保持独立术语）
    add("遗精", "症状", ["梦遗", "失精"])
    add("脱发", "症状", ["掉发", "掉头发"])
    add("前列腺炎", "症状", ["前列腺", "慢前", "慢性前列腺炎"])
    add("社恐", "症状", ["社交恐惧", "社交恐惧症"])
    add("痤疮", "症状", ["青春痘", "痘痘"])
    add("焦虑症", "症状", ["焦虑"])
    add("强迫症", "症状", ["强迫"])
    add("抑郁症", "症状", ["抑郁"])
    add("失眠", "症状", ["睡不着", "睡眠问题", "睡眠障碍"])
    add("肾虚", "症状", ["伤肾"])
    add("神经症", "症状", ["神经衰弱"])
    add("意淫", "因素", ["YY", "邪念", "邪淫"])
    add("看黄", "因素", ["搜黄", "黄片", "色情", "擦边"])
    add("淫梦", "因素", ["春梦", "黄梦", "梦中撸管"])
    add("手淫", "因素", ["SY", "撸管", "自渎", "恶习"])
    add("觉悟", "概念", ["觉察", "觉知"])
    add("破戒", "概念", ["复破", "复发"])
    add("性功能", "概念", ["性能力"])
    add("断念口诀", "方法", ["口诀"])
    return terms


def _mine_titles(cur, base: dict[str, dict]) -> dict[str, dict]:
    """《戒为良药》章节标题挖掘：「X问题/X障碍」→ 症状，「X破戒」→ 因素（破戒诱因）。

    过滤：含连接词（的/与/和…）、命中 _TITLE_STOP、与已有术语或别名重复/互为子串。
    """
    cur.execute("SELECT DISTINCT chapter_title FROM chunks WHERE book_id='jwly'")
    titles = [r[0] for r in cur.fetchall()]
    known_aliases = {a for v in base.values() for a in v["aliases"]} | set(base)
    mined: dict[str, dict] = {}
    for t in titles:
        for pattern, ttype in ((_RE_TITLE_PROBLEM, "症状"), (_RE_TITLE_FACTOR, "因素")):
            for m in pattern.finditer(t):
                x = m.group(1)
                if len(x) < 2 or x in _TITLE_STOP or _RE_TITLE_JUNK.search(x):
                    continue
                if x in known_aliases:
                    continue
                # 与已有术语互为子串（发育 ⊂ 发育障碍、紧张 ⊂ 紧张性遗精）则丢弃
                if any((x in k) or (k in x) for k in known_aliases):
                    continue
                known_aliases.add(x)
                mined.setdefault(x, {"type": ttype, "aliases": []})
    return mined


def _mine_tfidf(cur, top_n: int = 60, df_min: int = 25, df_max: int = 1500) -> dict[str, dict]:
    """chunks.tokens 的 TF-IDF 高权词 → 概念候选（中频领域词；停用表 + 词长过滤）。"""
    cur.execute("SELECT tokens FROM chunks WHERE dup_of IS NULL")
    docs = [json.loads(r[0]) for r in cur.fetchall()]
    n_docs = len(docs)
    df: Counter = Counter()
    tf: Counter = Counter()
    for toks in docs:
        df.update(set(toks))
        tf.update(toks)
    scored = []
    for tok, freq in tf.items():
        if len(tok) < 2 or tok in _TFIDF_STOP or not (df_min <= df[tok] <= df_max):
            continue
        scored.append((freq * math.log(n_docs / df[tok]), tok))
    scored.sort(reverse=True)
    mined: dict[str, dict] = {}
    for _, tok in scored[:top_n]:
        mined.setdefault(tok, {"type": "概念", "aliases": []})
    return mined


def generate(out_csv: Path) -> dict[str, dict]:
    conn = psycopg2.connect(**settings.dsn)
    try:
        with conn.cursor() as cur:
            terms = _base_terms()
            print(f"[1/3] 领域种子 + glossary：{len(terms)} 词条")
            titles = _mine_titles(cur, terms)
            for t, v in titles.items():
                terms[t] = v
            print(f"[2/3] 章节标题挖掘：+{len(titles)} 词条（共 {len(terms)}）")
            tfidf = _mine_tfidf(cur)
            new_tfidf = {t: v for t, v in tfidf.items() if t not in terms}
            for t, v in new_tfidf.items():
                terms[t] = v
            print(f"[3/3] TF-IDF 高权词：+{len(new_tfidf)} 词条（共 {len(terms)}）")
    finally:
        conn.close()

    # 过滤：TF-IDF/标题挖出的纯数字、单字符已在 add 内拦截；ASCII 统一小写规范词
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["term", "type", "aliases", "enabled"])
        for term in sorted(terms, key=lambda x: (terms[x]["type"], x)):
            v = terms[term]
            w.writerow([term, v["type"], ";".join(v["aliases"]), "true"])
    by_type: dict[str, int] = Counter(v["type"] for v in terms.values())
    print(f"CSV 草稿 → {out_csv}（{dict(by_type)}，可在表格中人工校对类型后 --import）")
    return terms


def import_csv(in_csv: Path) -> int:
    conn = psycopg2.connect(**settings.dsn)
    n = 0
    try:
        with conn.cursor() as cur, open(in_csv, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                term = (row.get("term") or "").strip()
                ttype = (row.get("type") or "概念").strip()
                if not term:
                    continue
                aliases = [a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()]
                enabled = (row.get("enabled") or "true").strip().lower() in ("true", "1", "是", "y")
                cur.execute(
                    "INSERT INTO terms (term, type, aliases, enabled) VALUES (%s,%s,%s,%s)"
                    " ON CONFLICT (term) DO UPDATE SET type=EXCLUDED.type,"
                    " aliases=EXCLUDED.aliases, enabled=EXCLUDED.enabled",
                    (term, ttype, json.dumps(aliases, ensure_ascii=False), enabled),
                )
                n += 1
        conn.commit()
    finally:
        conn.close()
    print(f"导入完成：{n} 词条 → terms 表")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="术语表种子（生成 CSV 草稿 / 导入 terms 表）")
    ap.add_argument("--gen", metavar="CSV", help="生成 CSV 草稿")
    ap.add_argument("--import", dest="imp", metavar="CSV", help="导入 CSV 到 terms 表")
    ap.add_argument("--auto", metavar="CSV", nargs="?", const="data/terms.csv",
                    help="生成并直接导入（免人工校对的基线）")
    args = ap.parse_args()
    if args.auto:
        out = Path(args.auto)
        out.parent.mkdir(parents=True, exist_ok=True)
        generate(out)
        import_csv(out)
    elif args.gen:
        generate(Path(args.gen))
    elif args.imp:
        import_csv(Path(args.imp))
    else:
        ap.print_help()

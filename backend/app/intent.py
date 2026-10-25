"""查询意图识别：规则存于 PostgreSQL(intent_rules 表)，内存缓存编译后的正则。

情绪类问题的最佳答案不只是方法论，还需要"有人挺过来了"的见证——
由检索层在尾部注入《戒者录》个人叙事（见 retriever._inject_narrative）。
规则通过管理后台增删改（内置规则不可删、可停用）。
"""
from __future__ import annotations

import re

from .db import conn_ctx

# 首次建库时的种子规则（builtin=1，不可删除、可停用）
DEFAULT_RULES = [
    r"绝望", r"崩溃", r"撑不下去", r"撑不住", r"坚持不下",
    r"没用", r"废人", r"自卑", r"恨自己", r"后悔死",
    r"想放弃", r"想死", r"活着没意思", r"破罐破摔", r"救救",
    r"心态崩", r"好累", r"太累了", r"又破了", r"一次又一次",
    r"戒不掉", r"屡戒屡破", r"怪圈", r"失败",
]

_CACHE: list[re.Pattern] = []


def refresh_intent_rules() -> None:
    """从库重载启用的规则（管理端增删改后调用）。"""
    global _CACHE
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT pattern FROM intent_rules WHERE enabled")
        _CACHE = [re.compile(r[0]) for r in cur.fetchall()]


def detect_intent(text: str) -> str:
    """返回 'emotional' | 'normal'。命中任一启用规则即判为情绪求助。"""
    if not _CACHE:
        refresh_intent_rules()
    for pat in _CACHE:
        if pat.search(text):
            return "emotional"
    return "normal"

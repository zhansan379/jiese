"""分块：段落级滑动窗口 + 修辞边界（案例三元组不拆散、【】知识块边界、目录锚点强制边界）。

每个 chunk 携带层级上下文（chapter/section 来自目录锚点）、
构成段落 para_ids（引用跳转高亮的依据）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .epub_parser import Book, Doc, Para, TocEntry

CHUNK_TARGET = 420   # 目标字数
CHUNK_MAX = 900      # 硬上限
OVERLAP_MIN = 50     # 重叠段最小字数
OVERLAP_MAX = 250    # 重叠段最大字数

_RE_CASE_START = re.compile(r"^(案例\s*\d*[：:]|【案例)")
_RE_MARK_HEAD = re.compile(r"^【[^】]{1,24}】$")
_RE_LEADIN = re.compile(r"^(下面[谈说]|下面[再来]*谈|接[着下来]谈|再来谈)")
_RE_ANSWER = re.compile(r"^(答|分析|回复)[：:]")
_RE_NUMBERED = re.compile(r"^（?\d+[）.、]")


@dataclass
class Chunk:
    id: str
    book_id: str
    doc_id: str
    seq: int
    para_ids: list[str]
    text: str
    chapter_title: str
    section_title: str | None
    author: str | None
    doc_type: str
    breadcrumb: str
    dup_of: str | None = None
    tokens: list[str] = field(default_factory=list)
    entities: dict = field(default_factory=dict)   # 实体标注 {"症状":[...],"方法":[...]}（build_kb 挂钩）

    @property
    def char_len(self) -> int:
        return len(self.text)


def _is_soft_heading(text: str) -> bool:
    """戒者录的伪标题：短、无句末标点、非问答/案例/列表前缀。"""
    if len(text) > 20 or re.search(r"[。！？；，：…]$", text):
        return False
    return not _RE_ANSWER.match(text) and not _RE_CASE_START.match(text) \
        and not _RE_NUMBERED.match(text) and not text.startswith("作者")


def _is_boundary_before(text: str) -> bool:
    return bool(_RE_CASE_START.match(text) or _RE_MARK_HEAD.match(text) or _RE_LEADIN.match(text))


def _classify_doc_type(chapter: str, section: str | None, author: str | None, book_author: str) -> str:
    titles = f"{chapter} {section or ''}"
    if author and author != book_author:
        return "个人叙事"
    if re.search(r"开示|印光|元音|禅不是|大手印|净土", titles):
        return "佛学开示"
    if re.search(r"案例", titles):
        return "案例分析"
    if re.search(r"口诀|德训|歌诀", titles):
        return "口诀德训"
    return "方法论"


def chunk_book(book: Book, id_start: int) -> list[Chunk]:
    """按文档页分块。id_start 为全局 chunk 序号起点（由调用方保证跨书递增）。"""
    chunks: list[Chunk] = []
    para2toc: dict[str, TocEntry] = {}
    for e in book.toc:
        if e.para_id:
            para2toc[e.para_id] = e
    doc_first_level1: dict[str, TocEntry | None] = {}
    for e in book.toc:
        if e.level == 1 and e.doc_id not in doc_first_level1:
            doc_first_level1[e.doc_id] = e

    for doc in book.docs:
        chapter = doc_first_level1.get(doc.id)
        cur_chapter = chapter.title if chapter else doc.title
        cur_section: str | None = None
        buf: list[Para] = []

        def flush(carry: bool = True) -> None:
            nonlocal buf
            if not buf:
                return
            text = "\n".join(p.text for p in buf)
            chunks.append(Chunk(
                id=f"c{len(chunks) + id_start:06d}",
                book_id=book.id, doc_id=doc.id, seq=len(chunks) + id_start,
                para_ids=[p.id for p in buf], text=text,
                chapter_title=cur_chapter, section_title=cur_section,
                author=doc.author or book.author,
                doc_type=_classify_doc_type(cur_chapter, cur_section, doc.author, book.author),
                breadcrumb=book.title + " › " + cur_chapter + (f" › {cur_section}" if cur_section else ""),
            ))
            # 重叠：携带末段进入下一 chunk
            if carry and buf:
                last = buf[-1]
                buf = [last] if OVERLAP_MIN <= len(last.text) <= OVERLAP_MAX else []
            else:
                buf = []

        for para in doc.paras:
            toc_e = para2toc.get(para.id)
            if para.kind == "heading" and toc_e is not None:
                flush(carry=False)
                if toc_e.level == 1:
                    cur_chapter = toc_e.title
                    cur_section = None
                else:
                    cur_section = toc_e.title
                continue
            text = para.text
            buf_len = sum(len(p.text) for p in buf)

            if _is_boundary_before(text) and buf_len >= 200:
                flush()
            elif _is_soft_heading(text) and buf_len >= CHUNK_TARGET:
                flush()

            buf.append(para)
            buf_len = sum(len(p.text) for p in buf)

            if _RE_ANSWER.match(text) and buf_len >= CHUNK_TARGET:
                flush()
            elif buf_len >= CHUNK_MAX:
                flush()
            elif buf_len >= CHUNK_TARGET and not _RE_ANSWER.match(text):
                flush()
        flush(carry=False)
    return chunks

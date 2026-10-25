"""EPUB 解析：EPUB(OEBPS zip) → 书 / 文档页 / 段落 / 目录。

- 一个 EPUB 内容文件(xhtml) = 一个阅读页(doc)
- 段落是阅读与引用跳转的最小单位，全局唯一 para_id
- NCX 目录节点解析为 toc 记录，锚点(anchor)落到具体段落
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

XHTML = "{http://www.w3.org/1999/xhtml}"
OPF_NS = {"o": "http://www.idpf.org/2007/opf"}
NCX_NS = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}

HEADING_TAGS = {f"{XHTML}h{i}" for i in range(1, 7)}
PARA_TAGS = {XHTML + t for t in ("p", "blockquote", "li", "pre")}
_SKIP_TEXT = re.compile(r"^[\s\u3000·—－\-.*●〇]*$")


@dataclass
class Para:
    id: str
    doc_id: str
    seq: int          # 文档内序号
    kind: str         # heading | para
    text: str
    elem_id: str | None = None


@dataclass
class Doc:
    id: str
    book_id: str
    seq: int
    file: str
    title: str
    author: str | None
    paras: list[Para] = field(default_factory=list)


@dataclass
class TocEntry:
    id: str
    book_id: str
    doc_id: str
    para_id: str | None
    title: str
    level: int
    seq: int


@dataclass
class Book:
    id: str
    title: str
    author: str
    docs: list[Doc] = field(default_factory=list)
    toc: list[TocEntry] = field(default_factory=list)

    @property
    def doc_count(self) -> int:
        return len(self.docs)

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for d in self.docs for p in d.paras)


class _IdAlloc:
    """全局自增 ID 分配器，保证跨书唯一。"""

    def __init__(self) -> None:
        self.doc = 0
        self.para = 0
        self.toc = 0

    def next_doc(self) -> str:
        self.doc += 1
        return f"d{self.doc:05d}"

    def next_para(self) -> str:
        self.para += 1
        return f"p{self.para:07d}"

    def next_toc(self) -> str:
        self.toc += 1
        return f"t{self.toc:05d}"


def _norm_href(href: str) -> str:
    href = href.strip().replace("\\", "/")
    return posixpath.normpath(href) if not href.startswith("/") else href


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_epub(path: str, book_id: str, title: str, author: str, alloc: _IdAlloc) -> Book:
    book = Book(id=book_id, title=title, author=author)
    zf = zipfile.ZipFile(path)

    # OPF 位置
    container = etree.fromstring(zf.read("META-INF/container.xml"))
    opf_path = container.find(".//c:rootfile", CONTAINER_NS).get("full-path")
    opf_dir = posixpath.dirname(opf_path)
    opf = etree.fromstring(zf.read(opf_path))

    # manifest: id -> (href, media_type)
    manifest = {}
    for item in opf.findall(".//o:manifest/o:item", OPF_NS):
        href = _norm_href(item.get("href"))
        if opf_dir and not href.startswith("/"):
            href = _norm_href(posixpath.join(opf_dir, href))
        manifest[item.get("id")] = (href, (item.get("media-type") or "").lower())

    # spine 顺序 → docs
    doc_by_href: dict[str, Doc] = {}
    for itemref in opf.findall(".//o:spine/o:itemref", OPF_NS):
        entry = manifest.get(itemref.get("idref"))
        if not entry:
            continue
        href, mtype = entry
        if "html" not in mtype and "xml" not in mtype:
            continue
        if href not in zf.namelist():
            continue
        doc = Doc(
            id=alloc.next_doc(), book_id=book_id, seq=len(book.docs),
            file=href, title="", author=None,
        )
        _parse_doc_paras(zf, href, doc, alloc)
        if doc.paras:
            book.docs.append(doc)
            doc_by_href[href] = doc
            doc_by_href.setdefault(posixpath.basename(href), doc)

    # NCX 目录
    ncx_name = _find_ncx(zf)
    if ncx_name:
        root = etree.fromstring(zf.read(ncx_name))
        entries: list[tuple[int, int, str, str, str | None]] = []
        _walk_navpoints(root.find(".//n:navMap", NCX_NS), 1, entries)
        entries.sort(key=lambda e: e[0])
        for play_order, level, label, src, anchor in entries:
            file_part = src.split("#", 1)[0] if src else ""
            doc = doc_by_href.get(_norm_href(file_part)) or doc_by_href.get(posixpath.basename(file_part))
            if doc is None:
                continue
            para_id = None
            if anchor:
                para_id = next((p.id for p in doc.paras if p.elem_id == anchor), None)
            if para_id is None:
                para_id = doc.paras[0].id if doc.paras else None
            book.toc.append(TocEntry(
                id=alloc.next_toc(), book_id=book_id, doc_id=doc.id,
                para_id=para_id, title=_clean_text(label), level=level,
                seq=len(book.toc),
            ))

    _assign_titles_and_authors(book)
    return book


def _find_ncx(zf: zipfile.ZipFile) -> str | None:
    for name in zf.namelist():
        if name.lower().endswith(".ncx"):
            return name
    return None


def _walk_navpoints(node, level: int, out: list) -> None:
    if node is None:
        return
    for np in node.findall("n:navPoint", NCX_NS):
        label_el = np.find("n:navLabel/n:text", NCX_NS)
        content_el = np.find("n:content", NCX_NS)
        if label_el is None or content_el is None:
            continue
        label = label_el.text or ""
        src = content_el.get("src") or ""
        anchor = src.split("#", 1)[1] if "#" in src else None
        try:
            play_order = int(np.get("playOrder", "0"))
        except ValueError:
            play_order = 0
        out.append((play_order, level, label, src.split("#")[0], anchor))
        _walk_navpoints(np, level + 1, out)


def _parse_doc_paras(zf: zipfile.ZipFile, href: str, doc: Doc, alloc: _IdAlloc) -> None:
    root = etree.fromstring(zf.read(href))
    seq = 0
    for el in root.iter():
        tag = el.tag
        if not isinstance(tag, str) or not tag.startswith(XHTML):
            continue
        text = _clean_text("".join(el.itertext()))
        if not text or _SKIP_TEXT.match(text):
            continue
        if tag in HEADING_TAGS:
            kind = "heading"
        elif tag in PARA_TAGS:
            kind = "para"
        elif tag == XHTML + "div":
            # calibre 转制的 div 若不含块级子元素则视为段落，否则跳过（避免与 p 重复）
            if el.find(f".//{XHTML}p") is not None or el.find(f".//{XHTML}div") is not None:
                continue
            kind = "para"
        else:
            continue
        if kind == "para" and doc.paras and doc.paras[-1].text == text:
            continue  # 嵌套元素去重
        seq += 1
        doc.paras.append(Para(
            id=alloc.next_para(), doc_id=doc.id, seq=seq,
            kind=kind, text=text, elem_id=el.get("id"),
        ))


def _assign_titles_and_authors(book: Book) -> None:
    toc_by_doc: dict[str, list[TocEntry]] = {}
    for e in book.toc:
        toc_by_doc.setdefault(e.doc_id, []).append(e)
    for doc in book.docs:
        entries = toc_by_doc.get(doc.id, [])
        level1 = [e for e in entries if e.level == 1]
        if level1:
            doc.title = level1[0].title
        else:
            heading = next((p.text for p in doc.paras if p.kind == "heading"), None)
            doc.title = heading or (entries[0].title if entries else "正文")
        # 署名作者（戒者录各篇的 byline）
        for p in doc.paras[:8]:
            m = re.match(r"^作者[：:]\s*(\S{1,24})", p.text)
            if m:
                doc.author = m.group(1)
                break

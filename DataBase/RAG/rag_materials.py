"""Extract text from the files accepted by the local RAG writer."""

import codecs
from collections.abc import Iterable
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from rag_db import RagError


TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
                 ".log", ".xml", ".yaml", ".yml", ".html", ".htm")
SUPPORTED_SUFFIXES = (*TEXT_SUFFIXES, ".docx", ".pdf")
MAX_FILE_BYTES = 20 * 1024 * 1024


class _HTMLText(HTMLParser):
    HIDDEN = {"head", "script", "style", "noscript", "template"}
    BLOCK = {"br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li",
             "tr", "section", "article", "pre", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.hidden: str | None = None

    def handle_starttag(self, tag, attrs):
        if self.hidden is not None:
            return
        if tag in self.HIDDEN:
            self.hidden = tag
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None
        elif self.hidden is None and tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden is None:
            self.parts.append(data)


def _join_blocks(parts: Iterable[str]) -> str:
    return "\n".join(filter(None, map(str.strip, parts)))


def _decode_text(data: bytes) -> str:
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encodings = ("utf-32",)
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ("utf-16",)
    elif data.startswith(codecs.BOM_UTF8):
        encodings = ("utf-8-sig",)
    else:
        encodings = ("utf-8", "gb18030")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RagError("文件编码无法识别，请另存为 UTF-8 文本")


def _word_blocks(data: bytes) -> Iterable[str]:
    from docx import Document
    from docx.table import Table

    document = Document(BytesIO(data))
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                yield "\t".join(cell.text for cell in row.cells)
        else:
            yield block.text


def _pdf_blocks(data: bytes) -> Iterable[str]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        raise RagError("请先解除 PDF 加密")
    for page in reader.pages:
        yield page.extract_text() or ""


def load_material(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise RagError(f"暂不支持 {suffix or '无扩展名'} 文件")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise RagError("文件超过 20 MiB")

    if suffix in {".docx", ".pdf"}:
        try:
            blocks = _word_blocks(data) if suffix == ".docx" else _pdf_blocks(data)
            text = _join_blocks(blocks)
        except RagError:
            raise
        except ImportError:
            raise RagError("请先安装 requirements.txt 中的文档解析依赖") from None
        except Exception:
            raise RagError(f"{suffix.upper()[1:]} 文件无法解析") from None
    else:
        text = _decode_text(data)
        if suffix in {".html", ".htm"}:
            parser = _HTMLText()
            try:
                parser.feed(text)
                parser.close()
            except (AssertionError, ValueError):
                raise RagError("HTML 文件无法解析") from None
            text = _join_blocks("".join(parser.parts).splitlines())

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\0" in text:
        raise RagError("文件包含无法写入的二进制内容")
    if not text:
        detail = "PDF 需要可提取的文字层" if suffix == ".pdf" else "文件中没有可写入的正文"
        raise RagError(detail)
    return text

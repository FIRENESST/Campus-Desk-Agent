"""CPU OCR for images and scanned PDFs received through MCP."""

import asyncio
import base64
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from math import isfinite
from threading import Lock
from typing import Annotated, Literal

import httpx2
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from domain_page_reader import DomainPageError, fetch_public_https


_MAX_INLINE_BYTES = 2 * 1024 * 1024
_MAX_REMOTE_BYTES = 10 * 1024 * 1024
_MAX_PAGES = 5
_MAX_IMAGE_PIXELS = 16_000_000
_MAX_RENDER_SIDE = 2_200
_MAX_PAGE_CHARS = 3_000
_MAX_PAGE_LINES = 120
_MAX_PAGE_WORDS = 160
_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "TIFF", "BMP", "GIF")
_MEDIA_TYPES = (
    "image/png", "image/jpeg", "image/webp", "image/tiff", "image/bmp", "image/x-ms-bmp",
    "image/gif", "application/pdf", "application/octet-stream",
)
_OCR_LOCK = Lock()

Detail = Literal["text", "lines", "words"]
PageNumber = Annotated[int, Field(strict=True, ge=1)]


class OcrError(Exception):
    pass


class OcrSpan(BaseModel):
    text: str
    confidence: float
    box: list[tuple[int, int]] = Field(min_length=4, max_length=4)


class OcrLine(OcrSpan):
    words: list[OcrSpan] | None = None


class OcrPage(BaseModel):
    page: int
    width: int
    height: int
    text: str
    lines: list[OcrLine] | None = None
    truncated: bool = False


class OcrResult(BaseModel):
    format: str
    source_url: str | None
    page_count: int
    text: str
    pages: list[OcrPage]
    truncated: bool


@dataclass(frozen=True)
class _Options:
    pages: list[int] | None
    detail: Detail
    min_confidence: float
    rotation: int


@lru_cache(maxsize=1)
def _engine():
    import cv2
    from rapidocr import ModelType, OCRVersion, RapidOCR

    cv2.setNumThreads(1)
    return RapidOCR(params={
        "Global.log_level": "error",
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Det.model_type": ModelType.SMALL,
        "Det.limit_type": "max",
        "Det.limit_side_len": 2_000,
        "Det.max_candidates": 400,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Rec.model_type": ModelType.SMALL,
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })


def _inline_data(value: str) -> bytes:
    if value.startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or not header.lower().endswith(";base64"):
            raise OcrError("请使用有效的 Base64 文件数据")
    try:
        data = base64.b64decode("".join(value.split()), validate=True)
    except ValueError:
        raise OcrError("请使用有效的 Base64 文件数据") from None
    if not data:
        raise OcrError("OCR 文件为空")
    if len(data) > _MAX_INLINE_BYTES:
        raise OcrError("Base64 文件超过 2 MiB")
    return data


def _page_numbers(count: int, requested: list[int] | None) -> list[int]:
    if not count:
        raise OcrError("文件没有可识别的页面")
    if requested is None:
        return list(range(1, min(count, _MAX_PAGES) + 1))
    if (
        not 1 <= len(requested) <= _MAX_PAGES or len(requested) != len(set(requested))
        or any(not 1 <= number <= count for number in requested)
    ):
        raise OcrError(f"请选择 1 到 {count} 范围内的页码，每次最多 {_MAX_PAGES} 个且不重复")
    return sorted(requested)


def _check_image_size(image) -> None:
    if image.width * image.height > _MAX_IMAGE_PIXELS:
        raise OcrError("图片超过 1600 万像素，请缩小后再识别")


def _prepare_image(image, rotation: int):
    from PIL import Image, ImageOps

    _check_image_size(image)
    image = ImageOps.exif_transpose(image)
    if rotation:
        image = image.rotate(-rotation, expand=True)
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _points(box, width: int, height: int) -> list[tuple[int, int]]:
    return [
        (min(width - 1, max(0, round(float(x)))), min(height - 1, max(0, round(float(y)))))
        for x, y in box
    ]


def _recognize_page(image, number: int, options: _Options) -> OcrPage:
    image = _prepare_image(image, options.rotation)
    width, height = image.size
    engine = _engine()
    wants_words = options.detail == "words"
    try:
        result = engine(image, text_score=options.min_confidence, return_word_box=wants_words)
    except IndexError:
        if not wants_words:
            raise
        # RapidOCR may omit a word entry for a detected line. Preserve the line result.
        result = engine(image, text_score=options.min_confidence, return_word_box=False)

    lines = None if options.detail == "text" else []
    texts = []
    chars = 0
    words_left = _MAX_PAGE_WORDS
    truncated = False
    if getattr(result, "txts", None):
        words_by_line = result.word_results
        words_aligned = wants_words and len(words_by_line) == len(result.txts)
        for index, (text, score, box) in enumerate(zip(result.txts, result.scores, result.boxes, strict=True)):
            text = text.strip()
            if not text:
                continue
            next_size = chars + len(text) + bool(texts)
            if len(texts) == _MAX_PAGE_LINES or next_size > _MAX_PAGE_CHARS:
                truncated = True
                break
            texts.append(text)
            chars = next_size
            if lines is None:
                continue

            words = None
            if words_aligned and words_by_line[index]:
                raw_words = words_by_line[index]
                words = [
                    OcrSpan(text=value, confidence=round(float(confidence), 4), box=_points(points, width, height))
                    for value, confidence, points in raw_words[:words_left]
                ]
                words_left -= len(words)
                truncated |= len(words) < len(raw_words)
            lines.append(OcrLine(text=text, confidence=round(float(score), 4), box=_points(box, width, height),
                                 words=words))
    return OcrPage(page=number, width=width, height=height, text="\n".join(texts), lines=lines,
                   truncated=truncated)


def _read_image(data: bytes, options: _Options) -> tuple[str, int, list[OcrPage]]:
    from PIL import Image

    try:
        with Image.open(BytesIO(data), formats=_IMAGE_FORMATS) as image:
            _check_image_size(image)
            image_format = image.format.lower()
            count = getattr(image, "n_frames", 1)
            pages = []
            for number in _page_numbers(count, options.pages):
                image.seek(number - 1)
                pages.append(_recognize_page(image, number, options))
            return image_format, count, pages
    except (Image.DecompressionBombError, OSError, EOFError):
        raise OcrError("图片无法读取。请提供有效的 PNG、JPEG、WebP、TIFF、BMP 或 GIF") from None


def _read_pdf(data: bytes, options: _Options) -> tuple[str, int, list[OcrPage]]:
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        raise OcrError("PDF 无法打开，请检查文件或解除密码保护") from None
    with closing(document):
        count = len(document)
        pages = []
        for number in _page_numbers(count, options.pages):
            try:
                with closing(document[number - 1]) as page:
                    width, height = page.get_size()
                    if not all(isfinite(side) and side > 0 for side in (width, height)):
                        raise OcrError(f"PDF 第 {number} 页尺寸无效")
                    scale = min(2.5, _MAX_RENDER_SIDE / max(width, height))
                    with closing(page.render(scale=scale)) as bitmap:
                        pages.append(_recognize_page(bitmap.to_pil(), number, options))
            except pdfium.PdfiumError:
                raise OcrError(f"PDF 第 {number} 页无法渲染") from None
        return "pdf", count, pages


def _recognize(data: bytes, source_url: str | None, options: _Options) -> OcrResult:
    # RapidOCR mutates per-call settings, and PDFium needs serialized access.
    if not _OCR_LOCK.acquire(blocking=False):
        raise OcrError("OCR 正忙，请稍后重试")
    try:
        if data.startswith(b"%PDF-"):
            file_format, count, pages = _read_pdf(data, options)
        else:
            file_format, count, pages = _read_image(data, options)
    finally:
        _OCR_LOCK.release()
    return OcrResult(
        format=file_format, source_url=source_url, page_count=count,
        text="\n\n".join(page.text for page in pages if page.text), pages=pages,
        truncated=(options.pages is None and len(pages) < count) or any(page.truncated for page in pages),
    )


async def ocr_document(
    url: Annotated[str, Field(strict=True, min_length=1, max_length=2_048,
                              description="公开 HTTPS 图片或 PDF 的直链")] | None = None,
    data_base64: Annotated[str, Field(strict=True, min_length=1,
                                     max_length=4 * ((_MAX_INLINE_BYTES + 2) // 3) + 128,
                                     description="由调用端提供的图片或 PDF Base64")] | None = None,
    pages: Annotated[list[PageNumber], Field(min_length=1, max_length=_MAX_PAGES,
                                            description="从 1 开始的页码或帧序号")] | None = None,
    detail: Annotated[Detail, Field(description="返回纯文本、行框，或行框和可用的词框")] = "lines",
    min_confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False,
                                         description="保留文字行的置信度下限")] = 0.5,
    rotation: Annotated[Literal[0, 90, 180, 270], Field(description="每页顺时针旋转的角度")] = 0,
) -> OcrResult:
    """本地识别图片或扫描 PDF 中的中英文，支持全文、行坐标及置信度。url 与 data_base64 二选一。"""
    if (url is None) == (data_base64 is None):
        raise ToolError("请在 url 和 data_base64 中提供一种文件来源")
    try:
        source_url = None
        if url is not None:
            resource = await fetch_public_https(httpx2.URL(url.strip()), media_types=_MEDIA_TYPES,
                                                max_bytes=_MAX_REMOTE_BYTES)
            if resource.truncated:
                raise OcrError("远程文件超过 10 MiB")
            data, source_url = resource.body, str(resource.url)
        else:
            data = _inline_data(data_base64)
        options = _Options(pages, detail, min_confidence, rotation)
        return await asyncio.to_thread(_recognize, data, source_url, options)
    except (DomainPageError, OcrError) as exc:
        raise ToolError(str(exc)) from None
    except httpx2.InvalidURL:
        raise ToolError("请提供有效的公开 HTTPS 文件地址") from None
    except ImportError:
        raise ToolError("请在 MCP 服务的虚拟环境安装 requirements.txt 中的 OCR 依赖") from None

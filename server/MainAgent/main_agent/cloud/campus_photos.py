"""User submissions and approved campus photo galleries."""

import asyncio
import hashlib
import json
import os
import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps
from psycopg.errors import UndefinedTable
from pydantic import BaseModel, ConfigDict, Field

from .errors import ServiceError, not_found


MAX_PHOTO_UPLOAD_MB = 20
MAX_PHOTO_UPLOAD = MAX_PHOTO_UPLOAD_MB * 1024 * 1024
MAX_STORED_PHOTO = 2 * 1024 * 1024
PHOTO_PAGE = 12
REVIEW_PAGE = 20
_PLACE = re.compile(r"([0-9]{7})_([0-9]{6})\Z")
_MAP_ROOT = Path(__file__).resolve().parents[4] / "DataBase/CampusNavigation/local"


class PhotoReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["approved", "rejected"]
    version: int = Field(ge=1, le=2_147_483_647)


@lru_cache(maxsize=1)
def _map_features():
    root = Path(os.getenv("CAMPUS_NAVIGATION_DIR") or _MAP_ROOT)
    return json.loads((root / "base_map.geojson").read_text(encoding="utf-8"))["features"]


@lru_cache(maxsize=1)
def _campus_ring():
    try:
        boundary = next(item for item in _map_features() if item["properties"]["kind"] == "campus_boundary")
        return boundary["geometry"]["coordinates"][0]
    except (OSError, ValueError, KeyError, StopIteration, IndexError):
        raise ServiceError(503, "campus_photo_map_unavailable", "暂时无法核对校园点位") from None


@lru_cache(maxsize=1)
def _place_names():
    names = {}
    for feature in _map_features():
        geometry = feature["geometry"]
        if geometry["type"] == "Point" and (name := feature["properties"].get("name")):
            lon, lat = geometry["coordinates"][:2]
            place = f"{int(lon * 10000 + 0.5)}_{int(lat * 10000 + 0.5)}"
            names.setdefault(place, name)
    return names


def _contains(lon, lat, ring):
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def check_place(place):
    match = _PLACE.fullmatch(place)
    if not match:
        raise ServiceError(422, "invalid_photo_place", "地图点位无效")
    lon, lat = (int(value) / 10000 for value in match.groups())
    ring = _campus_ring()
    # A chosen point can round into the adjacent cell at a campus entrance.
    half_cell = 0.00005
    if not any(_contains(lon + dx, lat + dy, ring) for dx in (-half_cell, 0, half_cell)
               for dy in (-half_cell, 0, half_cell)):
        raise ServiceError(422, "invalid_photo_place", "请选择校区范围内的拍摄点")
    return place


def page_offset(value):
    if not re.fullmatch(r"[0-9]{1,6}", value):
        raise ServiceError(422, "invalid_photo_page", "分页参数无效")
    return int(value)


def _clean_image(raw):
    try:
        with Image.open(BytesIO(raw), formats=["JPEG", "PNG", "WEBP"]) as source:
            if source.width * source.height > 24_000_000 or getattr(source, "is_animated", False):
                raise ValueError
            source.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            pixels = ImageOps.exif_transpose(source).convert("RGBA")
            clean = Image.new("RGB", pixels.size, "white")
            clean.paste(pixels, mask=pixels.getchannel("A"))
            output = BytesIO()
            clean.save(output, format="JPEG", quality=84, optimize=True)
            result = output.getvalue()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise ServiceError(422, "invalid_campus_photo", "请选择有效的 JPG、PNG 或 WebP 静态照片，像素总数须在 2400 万以内") from None
    if len(result) > MAX_STORED_PHOTO:
        raise ServiceError(413, "campus_photo_too_large", "压缩后的照片仍然过大，请换一张照片")
    return result


def _page(rows, offset, size):
    return {"items": rows[:size], "next_offset": offset + size if len(rows) > size else None}


class CampusPhotos:
    def __init__(self, database):
        self.db = database
        self._decoders = asyncio.Semaphore(2)

    async def _run(self, operation):
        try:
            return await self.db.run(operation)
        except UndefinedTable:
            raise ServiceError(503, "campus_photos_unavailable", "校园照片功能需要先执行数据库迁移 011") from None

    async def places(self):
        items = await self._run(lambda c: c.execute(
            """SELECT place,count(*) AS photo_count FROM public.campus_photos
               WHERE status='approved' GROUP BY place ORDER BY place""",
        ).fetchall())
        return {"items": items}

    async def gallery(self, place, offset):
        return _page(await self._run(lambda c: c.execute(
            """SELECT id,created_at FROM public.campus_photos WHERE place=%s AND status='approved'
               ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s""", (place, PHOTO_PAGE + 1, offset),
        ).fetchall()), offset, PHOTO_PAGE)

    async def submit(self, uploader, place, key, raw):
        if not 0 < len(raw) <= MAX_PHOTO_UPLOAD:
            raise ServiceError(413, "campus_photo_too_large", f"请选择一张不超过 {MAX_PHOTO_UPLOAD_MB} MB 的照片")
        digest = hashlib.sha256(raw).hexdigest()
        async with self._decoders:
            image = await asyncio.to_thread(_clean_image, raw)

        def save(connection):
            created = connection.execute(
                """INSERT INTO public.campus_photos(uploader_id,place,request_key,request_digest,image)
                   VALUES(%s,%s,%s,%s,%s) ON CONFLICT(uploader_id,request_key) DO NOTHING RETURNING id,status""",
                (uploader, place, key, digest, image),
            ).fetchone()
            if created:
                return created
            previous = connection.execute(
                """SELECT id,status,place,request_digest FROM public.campus_photos WHERE uploader_id=%s AND request_key=%s""",
                (uploader, key),
            ).fetchone()
            if previous["place"] != place or previous["request_digest"] != digest:
                raise ServiceError(409, "campus_photo_request_reused", "此投稿请求已用于其他照片或地点")
            return {"id": previous["id"], "status": previous["status"]}

        return await self._run(save)

    async def image(self, photo_id, *, approved_only=True):
        row = await self._run(lambda c: c.execute(
            "SELECT image FROM public.campus_photos WHERE id=%s AND (NOT %s OR status='approved')",
            (photo_id, approved_only),
        ).fetchone())
        if not row:
            raise not_found()
        return row["image"]

    async def review_places(self, status, offset):
        rows = await self._run(lambda c: c.execute(
            """SELECT place,count(*) AS photo_count FROM public.campus_photos
               WHERE (%s='all' OR status=%s) GROUP BY place ORDER BY place LIMIT %s OFFSET %s""",
            (status, status, REVIEW_PAGE + 1, offset),
        ).fetchall())
        try:
            names = _place_names()
        except (OSError, ValueError, KeyError, TypeError):
            names = {}
        return _page([{**row, "name": names.get(row["place"])} for row in rows], offset, REVIEW_PAGE)

    async def review_queue(self, status, offset, place=None):
        return _page(await self._run(lambda c: c.execute(
            """SELECT id,place,status,version,uploader_id,created_at,reviewed_at FROM public.campus_photos
               WHERE (%s='all' OR status=%s) AND (%s::varchar IS NULL OR place=%s)
               ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s""",
            (status, status, place, place, REVIEW_PAGE + 1, offset),
        ).fetchall()), offset, REVIEW_PAGE)

    async def review(self, photo_id, reviewer, decision):
        result = await self._run(lambda c: c.execute(
            """UPDATE public.campus_photos
               SET status=%s,reviewed_by=%s,reviewed_at=clock_timestamp(),version=version+1
               WHERE id=%s AND version=%s RETURNING id,status,version""",
            (decision.status, reviewer, photo_id, decision.version),
        ).fetchone())
        if not result:
            raise ServiceError(409, "campus_photo_review_stale", "照片状态已变更，请刷新后重新审核")
        return result

    async def delete(self, photo_id, version):
        result = await self._run(lambda c: c.execute(
            "DELETE FROM public.campus_photos WHERE id=%s AND version=%s RETURNING id", (photo_id, version),
        ).fetchone())
        if not result:
            raise ServiceError(409, "campus_photo_review_stale", "照片已变更或删除，请刷新后重试")
        return result

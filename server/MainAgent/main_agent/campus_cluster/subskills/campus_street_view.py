"""Server-authored content at campus waypoints, shared by the skill and HTTP API."""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl, AwareDatetime, BaseModel, ConfigDict, Field, JsonValue,
    StringConstraints, field_validator, model_validator,
)

from ...reliability.models import ToolResult
from .walking_routes import distance_m, resolve_walk
from ..skills import Skill, SkillContext, SkillInput


CAMPUS_VIEW_PATH = "/v1/campus-view"
_DEFAULT_ROOT = Path(__file__).resolve().parents[5] / "DataBase" / "CampusNavigation" / "local"
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".mp4": "video/mp4", ".webm": "video/webm",
}

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
MediaName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")]
Longitude = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
Latitude = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
Coordinate = tuple[Longitude, Latitude]


class StreetViewUnavailable(Exception):
    pass


class _ManifestData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class TextBlock(_ManifestData):
    kind: Literal["text"]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)]


class MediaBlock(_ManifestData):
    kind: Literal["image", "panorama", "video"]
    file: MediaName
    caption: Label | None = None

    @model_validator(mode="after")
    def validate_media_type(self):
        media_type = _MEDIA_TYPES.get(Path(self.file).suffix.lower(), "")
        expected = "video/" if self.kind == "video" else "image/"
        if not media_type.startswith(expected):
            raise ValueError("媒体文件扩展名与内容类型不符")
        return self


class LinkBlock(_ManifestData):
    kind: Literal["link"]
    title: Label
    url: AnyHttpUrl

    @field_validator("url")
    @classmethod
    def validate_url(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if url.scheme != "https" or url.username is not None or url.password is not None:
            raise ValueError("链接须为无凭据的 HTTPS 地址")
        return url


class CustomBlock(_ManifestData):
    kind: Literal["custom"]
    component: Slug
    props: dict[str, JsonValue]


ContentBlock = Annotated[TextBlock | MediaBlock | LinkBlock | CustomBlock, Field(discriminator="kind")]


class StreetViewPoint(_ManifestData):
    id: Slug
    name: Label
    coordinates: Coordinate
    aliases: tuple[Label, ...] = Field(default=(), max_length=12)
    captured_at: AwareDatetime | None = None
    heading_deg: Annotated[float, Field(ge=0, lt=360, allow_inf_nan=False)] | None = None
    content: tuple[ContentBlock, ...] = Field(default=(), max_length=20)

    def summary(self) -> dict:
        return {
            **self.model_dump(mode="json", exclude={"content"}, exclude_none=True),
            "content_count": len(self.content),
            "detail_path": f"{CAMPUS_VIEW_PATH}/points/{self.id}",
        }

    def detail(self) -> dict:
        blocks = []
        for block in self.content:
            value = block.model_dump(mode="json", exclude_none=True)
            if isinstance(block, MediaBlock):
                del value["file"]
                value["url"] = f"{CAMPUS_VIEW_PATH}/media/{block.file}"
            blocks.append(value)
        return {**self.summary(), "content": blocks}


class StreetViewRoute(_ManifestData):
    id: Slug
    name: Label
    point_ids: tuple[Slug, ...] = Field(min_length=1, max_length=2_000)
    path: tuple[Coordinate, ...] | None = Field(default=None, min_length=2, max_length=10_000)

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "point_count": len(self.point_ids),
                "detail_path": f"{CAMPUS_VIEW_PATH}/routes/{self.id}"}


class StreetViewCatalog(_ManifestData):
    version: Literal[1] = 1
    campus_id: Slug
    campus_name: Label
    points: tuple[StreetViewPoint, ...] = Field(default=(), max_length=2_000)
    routes: tuple[StreetViewRoute, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_references(self):
        point_ids = {point.id for point in self.points}
        if len(point_ids) != len(self.points) or len({route.id for route in self.routes}) != len(self.routes):
            raise ValueError("点位和路线的 ID 必须各自唯一")
        if any(point_id not in point_ids for route in self.routes for point_id in route.point_ids):
            raise ValueError("路线引用了未配置的点位")
        return self

    def summary(self) -> dict:
        return {
            "version": self.version, "campus_id": self.campus_id,
            "campus_name": self.campus_name, "coordinate_system": "WGS84",
            "points": [point.summary() for point in self.points],
            "routes": [route.summary() for route in self.routes],
        }

    def route(self, route_id: str) -> StreetViewRoute | None:
        return next((route for route in self.routes if route.id == route_id), None)

    def ordered_points(self, route: StreetViewRoute) -> tuple[StreetViewPoint, ...]:
        by_id = {point.id: point for point in self.points}
        return tuple(by_id[point_id] for point_id in route.point_ids)


class StreetViewInput(SkillInput):
    point: Label | None = Field(default=None, description="点位 ID、名称或别名")
    start: Label | None = Field(default=None, description="步行路线起点，填具体校门或校内地标")
    destination: Label | None = Field(default=None, description="步行路线终点，和 start 同时填写")
    longitude: Longitude | None = Field(default=None, description="WGS84 经度，和纬度一起传")
    latitude: Latitude | None = Field(default=None, description="WGS84 纬度，和经度一起传")
    route_id: Slug | None = Field(default=None, description="可选，只在此路线的点位里匹配")
    radius_m: Annotated[float, Field(ge=1, le=150, allow_inf_nan=False)] = 40

    @model_validator(mode="after")
    def validate_lookup(self):
        if (self.start is None) != (self.destination is None) or (
            self.start is not None and any(value is not None for value in (self.point, self.longitude, self.latitude, self.route_id))
        ):
            raise ValueError("查询步行路线只需同时填写起点和终点")
        has_coordinates = self.longitude is not None and self.latitude is not None
        if (self.longitude is None) != (self.latitude is None) or (self.point and has_coordinates):
            raise ValueError("可按名称或经纬度查询，经纬度需要同时提供")
        return self


def load_street_view() -> tuple[Path, StreetViewCatalog]:
    try:
        root = Path(os.getenv("CAMPUS_NAVIGATION_DIR") or os.getenv("CAMPUS_STREET_VIEW_DIR") or _DEFAULT_ROOT).resolve()
        with (root / "street_view.json").open("rb") as stream:
            raw = stream.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("点位配置超过大小限制")
        return root, StreetViewCatalog.model_validate_json(raw)
    except (OSError, ValueError):
        raise StreetViewUnavailable("街景点位配置暂不可用，请联系维护者") from None


def street_view_media(root: Path, catalog: StreetViewCatalog, name: str) -> tuple[Path, str] | None:
    if not any(isinstance(block, MediaBlock) and block.file == name
               for point in catalog.points for block in point.content):
        return None
    try:
        media = (root / "media").resolve()
        file = (media / name).resolve()
        if media.is_relative_to(root) and file.is_relative_to(media) and file.is_file():
            return file, _MEDIA_TYPES[Path(name).suffix.lower()]
    except (OSError, ValueError):
        pass
    return None


def resolve_street_view(catalog: StreetViewCatalog, query: StreetViewInput, root: Path | None = None) -> dict:
    response = {"campus_id": catalog.campus_id, "coordinate_system": "WGS84", "catalog_path": CAMPUS_VIEW_PATH}
    if query.start is not None:
        return {**response, **resolve_walk(root or _DEFAULT_ROOT, catalog.campus_id, query.start, query.destination)}
    points = catalog.points
    if query.route_id is not None:
        route = catalog.route(query.route_id)
        if route is None:
            return {**response, "status": "route_not_found"}
        response["route"] = route.summary()
        # A route can visit a waypoint more than once.
        by_id = {point.id: point for point in catalog.ordered_points(route)}
        points = tuple(by_id.values())
    if query.point is not None:
        term = query.point.casefold()
        for point in points:
            if point.id == term:
                return {**response, "status": "found", "point": point.summary()}
        matches = [point for point in points if term in {name.casefold() for name in (point.name, *point.aliases)}]
        if not matches:
            matches = [point for point in points if any(term in name.casefold() for name in (point.name, *point.aliases))]
        if len(matches) == 1:
            return {**response, "status": "found", "point": matches[0].summary()}
        if matches:
            return {**response, "status": "ambiguous", "candidates": [point.summary() for point in matches[:8]]}
        return {**response, "status": "no_match"}
    if query.longitude is not None and query.latitude is not None:
        ranked = min(((distance_m((query.longitude, query.latitude), point.coordinates), point.id, point)
                      for point in points), default=None)
        if ranked is not None and ranked[0] <= query.radius_m:
            return {**response, "status": "found", "point": ranked[2].summary(), "distance_m": round(ranked[0], 1)}
        return {**response, "status": "no_match"}
    return {**response, "status": "browse", "point_count": len(points)}


async def campus_street_view(_context: SkillContext, args: StreetViewInput) -> ToolResult:
    def lookup():
        root, catalog = load_street_view()
        return resolve_street_view(catalog, args, root)

    try:
        data = await asyncio.to_thread(lookup)
    except StreetViewUnavailable as exc:
        return ToolResult(json.dumps({"status": "unavailable", "message": str(exc)}, ensure_ascii=False), "error")
    if args.start is not None:
        # The API supplies geometry to the map. Chat only needs directions and source attribution.
        data.pop("path", None)
    return ToolResult(json.dumps(data, ensure_ascii=False), "success")


CAMPUS_STREET_VIEW = Skill(
    "campus_street_view",
    "规划中传北京校区步行路线，传入 start 和 destination，返回道路、方向、分段距离及来源。"
    "也可按点位 ID、名称或 WGS84 经纬度查询校园街景点位。",
    StreetViewInput, campus_street_view, read_only=True,
)

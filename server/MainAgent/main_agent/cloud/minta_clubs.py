"""Allowlisted club management through MINTA with a server-owned actor."""

from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError, model_validator

from ..campus_cluster.activity_time import ActivityTimestamp, validate_activity_range
from .campus_access import AcademicAudience
from .errors import ServiceError
from .minta_operations import GatewayRejected, GatewayUncertain, academic_viewer


Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")]
Version = Annotated[int, Field(ge=1, le=2_147_483_647)]
ArchiveSnapshot = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ClubInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ClubDetails(ClubInput):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    category: Annotated[str, Field(min_length=1, max_length=40)]
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    audience: AcademicAudience | None = None


class RegisterClub(ClubDetails):
    action: Literal["register"]


class UpdateClub(ClubDetails):
    action: Literal["update"]
    id: Identifier
    version: Version


class ArchiveClub(ClubInput):
    action: Literal["archive", "restore"]
    id: Identifier
    version: Version


class ActivityDetails(ClubInput):
    id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    location: Annotated[str, Field(min_length=1, max_length=120)]
    starts_at: ActivityTimestamp
    ends_at: ActivityTimestamp
    capacity: Annotated[int, Field(ge=1, le=10000)]

    @model_validator(mode="after")
    def ordered_times(self):
        validate_activity_range(self.starts_at, self.ends_at)
        return self


class PublishActivity(ActivityDetails):
    action: Literal["publish_activity"]


class UpdateActivity(ActivityDetails):
    action: Literal["update_activity"]
    activity_id: Identifier
    version: Version


class ArchiveActivity(ClubInput):
    action: Literal["archive_activity", "restore_activity"]
    id: Identifier
    activity_id: Identifier
    version: Version


class PurgeArchives(ClubInput):
    action: Literal["purge_archives"]
    snapshot: ArchiveSnapshot


class ArchiveRecord(ClubInput):
    version: Version
    activity_id: Identifier | None = None


class PurgeArchive(ArchiveRecord):
    action: Literal["purge_archive"]
    id: Identifier


class ClubCommand(RootModel[Annotated[
    RegisterClub | UpdateClub | ArchiveClub | PublishActivity | UpdateActivity | ArchiveActivity | PurgeArchives | PurgeArchive,
    Field(discriminator="action"),
]]):
    pass


class ManagedActivity(BaseModel):
    model_config = ConfigDict(strict=True)
    id: Identifier
    name: str
    description: str
    location: str
    starts_at: str
    ends_at: str | None = None
    capacity: int
    remaining: int
    points: Annotated[int, Field(ge=1, le=4)] | None = None
    version: int
    archived: bool


class ClubSummary(BaseModel):
    model_config = ConfigDict(strict=True)
    id: Identifier
    name: str
    category: str
    description: str
    registrant: str
    created_at: str
    version: int
    archived: bool
    can_manage: bool
    activities: list[ManagedActivity]
    review_status: Literal["pending", "approved", "rejected"] = "pending"
    audience: AcademicAudience = Field(default_factory=AcademicAudience)
    can_join: bool = True


class ClubReview(ClubInput):
    version: Version
    status: Literal["approved", "rejected"]


class ArchiveCounts(BaseModel):
    model_config = ConfigDict(strict=True)
    club_count: Annotated[int, Field(ge=0)]
    activity_count: Annotated[int, Field(ge=0)]


class ArchiveSummary(ArchiveCounts):
    snapshot: ArchiveSnapshot


class AdminArchiveCommand(ArchiveRecord):
    action: Literal["restore", "purge"]


class ArchiveRow(BaseModel):
    model_config = ConfigDict(strict=True)
    club_id: Identifier
    activity_id: Identifier | None
    club_name: str
    activity_name: str | None
    registrant: str
    version: Version


class ArchivePage(BaseModel):
    model_config = ConfigDict(strict=True)
    items: list[ArchiveRow] = Field(max_length=40)
    next_offset: Annotated[int, Field(ge=0)] | None


async def list_club_archives(gateway, offset):
    try:
        page = await gateway.request("GET", f"/club/managed/archives?offset={offset}")
        return ArchivePage.model_validate(page).model_dump()
    except GatewayRejected as exc:
        raise exc.as_service_error() from None
    except (GatewayUncertain, ValidationError):
        raise ServiceError(502, "club_archives_unavailable", "暂时无法读取社团归档") from None


async def change_club_archive(gateway, club_id, command, *, owner=None):
    path = f"/club/managed/archives/{club_id}"
    data = command.model_dump(include={"version", "activity_id"}, exclude_none=True)
    try:
        if owner is None:
            response = await gateway.request("PATCH", path, {**data, "action": command.action})
            if command.action == "restore":
                if response.get("restored") is not True:
                    raise GatewayUncertain
                return {"restored": True}
        else:
            response = await gateway.request("POST", path + "/purge", {**data, "actor_id": str(owner)})
        return ArchiveCounts.model_validate(response).model_dump()
    except GatewayRejected as exc:
        if exc.code == "CONFLICT":
            raise ServiceError(409, "club_archives_changed", "归档状态已变化，请刷新后再操作。") from None
        raise exc.as_service_error() from None
    except (GatewayUncertain, ValidationError):
        raise ServiceError(502, "club_archive_unconfirmed", "暂时无法确认归档处理结果，请刷新查看。") from None


async def review_clubs(gateway, *, offset=0, club_id=None, review=None):
    try:
        if review is None:
            response = await gateway.request("GET", f"/club/managed/review?offset={offset}")
            items = [ClubSummary.model_validate(club).model_dump() for club in response["clubs"]]
            return {"items": items, "next_offset": response["next_offset"]}
        response = await gateway.request("PATCH", f"/club/managed/review/{club_id}", review.model_dump())
        club = ClubSummary.model_validate(response["club"])
        if club.id != club_id or club.review_status != review.status:
            raise ValueError
        return {"club": club.model_dump()}
    except GatewayRejected as exc:
        if exc.code == "CONFLICT":
            raise ServiceError(409, "club_review_stale", "社团资料已更新，请刷新后重新审核") from None
        raise exc.as_service_error() from None
    except (GatewayUncertain, ValueError, KeyError, TypeError):
        raise ServiceError(502, "club_review_unconfirmed", "社团后端暂不可用，请刷新后确认审核结果") from None


async def list_clubs(gateway, user_id, viewer=None, *, include_archive_summary=False):
    try:
        if viewer is None:
            response = await gateway.request("GET", f"/club/managed/clubs?actor_id={user_id}")
        else:
            response = await gateway.request("POST", "/club/managed/directory", {
                "actor_id": str(user_id), "viewer": academic_viewer(viewer),
            })
        if not isinstance(response.get("clubs"), list):
            raise ValueError
        result = {"items": [ClubSummary.model_validate(club).model_dump() for club in response["clubs"]]}
        if include_archive_summary and "archive_summary" in response:
            result["archive_summary"] = ArchiveSummary.model_validate(response["archive_summary"]).model_dump()
        return result
    except GatewayRejected as exc:
        raise exc.as_service_error() from None
    except (GatewayUncertain, ValueError, ValidationError):
        raise ServiceError(502, "club_unavailable", "社团资料暂不可读取") from None


async def purge_club_archives(gateway, user_id, snapshot):
    try:
        response = await gateway.request("POST", "/club/managed/archives/purge", {
            "actor_id": str(user_id), "snapshot": snapshot,
        })
        return ArchiveCounts.model_validate(response).model_dump()
    except GatewayRejected as exc:
        if exc.code == "CONFLICT":
            raise ServiceError(409, "club_archives_changed", "归档清单已更新，请刷新后重新确认。") from None
        raise exc.as_service_error() from None
    except (GatewayUncertain, ValidationError):
        raise ServiceError(502, "club_purge_unconfirmed", "暂时无法确认清理结果，请刷新归档清单。") from None


async def change_club(gateway, user_id, registrant, key, command: ClubCommand, *, effect_id=None):
    data = command.root.model_dump(exclude_none=True)
    data["actor_id"] = str(user_id)
    if effect_id:
        data["effect_id"] = str(effect_id)
    if data["action"] == "register":
        data["id"] = str(uuid5(user_id, f"minta-club:{key}"))
        data["registrant"] = registrant
    elif data["action"] == "publish_activity":
        data["activity_id"] = str(uuid5(UUID(data["id"]), f"minta-activity:{user_id}:{key}"))
    try:
        response = await gateway.request("POST", "/club/managed/clubs", data)
        club = ClubSummary.model_validate(response.get("club"))
        if club.id != data["id"] or not club.can_manage:
            raise ValueError
        result = {"club": club.model_dump()}
        if effect_id:
            if response.get("effect") not in ("applied", "no_effect"):
                raise ValueError
            result["effect"] = response["effect"]
        return result
    except GatewayRejected as exc:
        if exc.code == "CONFLICT" and data["action"] in {"restore", "restore_activity"}:
            raise ServiceError(409, "club_archives_changed", "归档状态已变化，请刷新后再操作。") from None
        raise exc.as_service_error() from None
    except (GatewayUncertain, ValueError, ValidationError):
        raise ServiceError(502, "club_write_unconfirmed", "社团操作结果未确认，请刷新查看当前资料") from None


async def club_effect(gateway, user_id, effect_id, *, undo=False):
    path = f"/club/managed/effects/{effect_id}"
    try:
        if undo:
            reply = await gateway.request("POST", path, {"actor_id": str(user_id)})
        else:
            reply = await gateway.request("GET", f"{path}?actor_id={user_id}")
        state = reply.get("effect")
        if state not in ("applied", "no_effect", "undone") or (undo and state not in ("no_effect", "undone")):
            raise GatewayUncertain
        return state
    except GatewayRejected as exc:
        if exc.code == "CONFLICT":
            raise ServiceError(409, "club_undo_conflict", "社团或活动已有新变动，暂时无法恢复。") from None
        raise exc.as_service_error() from None
    except GatewayUncertain:
        raise ServiceError(502, "club_undo_unconfirmed", "暂时无法确认社团操作，请稍后重试。") from None

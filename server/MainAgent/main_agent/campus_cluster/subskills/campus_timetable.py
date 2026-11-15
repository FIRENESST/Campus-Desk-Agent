"""Read the authenticated campus services' current timetable."""

from datetime import date, timedelta
from typing import Literal

from pydantic import Field, model_validator

from ...reliability.models import ToolResult
from ..skills import Skill, SkillInput


MAX_TIMETABLE_DAYS = 31
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
CLOCK_PATTERN = r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"
END_CLOCK_PATTERN = r"^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)$"


class CampusTimetableInput(SkillInput):
    service: Literal["classroom", "gym", "activity", "schedule", "personal", "library", "my_loans"] = Field(
        description="教室、体育场馆、校园活动、本人课程/授课、个人日程、匿名馆藏借期、本人借期",
    )
    date: str | None = Field(default=None, pattern=DATE_PATTERN, description="北京时间起始日期。可省略为今天，借期默认查所有当前借阅")
    end_date: str | None = Field(default=None, pattern=DATE_PATTERN, description="含当日的结束日期，最多查 31 天")
    start_time: str = Field(default="00:00", pattern=CLOCK_PATTERN, description="每天查询的起始钟点，HH:MM")
    end_time: str = Field(default="24:00", pattern=END_CLOCK_PATTERN, description="每天查询的结束钟点，HH:MM")
    target: str = Field(default="", max_length=120, description="教室、场馆、活动、课程或书籍的名称或编号")
    available_only: bool = Field(default=False, description="教室或场馆只返回可预约的时段")
    min_minutes: int = Field(default=1, ge=1, le=1440, description="教室或场馆的连续可用分钟数下限")
    min_capacity: int = Field(default=0, ge=0, le=10000, description="教室容量下限")
    page: int = Field(default=1, ge=1, le=10000, description="时段表页码")

    @property
    def has_window(self):
        return self.date is not None or (self.start_time, self.end_time) != ("00:00", "24:00")

    @model_validator(mode="after")
    def validate_window(self):
        if self.start_time >= self.end_time:
            raise ValueError("时段结束须晚于开始")
        if self.date is None and self.end_date is not None:
            raise ValueError("日期范围需要起始日期")
        if self.date is not None:
            first = date.fromisoformat(self.date)
            last = date.fromisoformat(self.end_date or self.date)
            if not (date.min < first <= last < date.max - timedelta(days=1)
                    and (last - first).days < MAX_TIMETABLE_DAYS):
                raise ValueError("请选择有效的日期范围，最多 31 天")
        if self.service not in {"classroom", "gym"} and (self.available_only or self.min_minutes != 1):
            raise ValueError("可预约时段筛选适用于教室和体育场馆")
        if self.service != "classroom" and self.min_capacity:
            raise ValueError("容量筛选适用于教室")
        return self


async def _unbound(_context, _args):
    return ToolResult("请登录并完成校园身份认证后查询时段。", "error")


CAMPUS_TIMETABLE = Skill(
    "campus_timetable",
    "查询实时校务时段表。教室查询可预约空档、占用时间和容量；体育场馆查询场地号、起止时间和余量；"
    "activity 查询可见校园及社团活动；schedule 查询本人课程或授课；personal 查询本人课程、已报名活动、预约和自定义日程；"
    "library 查询匿名馆藏借期及到期时间；my_loans 查询本人借期。"
    "把今天、明天等相对日期按当前北京时间换算。所有时段以回执中的 starts_at、ends_at 为准。"
    "问哪间教室空闲时可不填 target，设置 available_only，并按需传入钟点、最少分钟数或人数。"
    "有下一页时继续查或说明当前页范围。",
    CampusTimetableInput, _unbound, read_only=True,
)

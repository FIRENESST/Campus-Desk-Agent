"""Timestamp rules for the campus activity catalog and management commands."""

import re
from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field

from .action_plan import CAMPUS_TIME_ZONE


TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ](?:[01][0-9]|2[0-3]):[0-5][0-9]"
    r"(?::[0-5][0-9](?:\.[0-9]{1,9})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?$"
)
ActivityTimestamp = Annotated[str, Field(max_length=80, pattern=TIMESTAMP_PATTERN)]


def campus_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=parsed.tzinfo or CAMPUS_TIME_ZONE)


def campus_minute(value: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP_PATTERN.fullmatch(value):
        raise ValueError("请输入日期和具体时间")
    parsed = campus_datetime(value)
    if parsed.second or parsed.microsecond:
        raise ValueError("请输入精确到分钟的时间")
    return parsed.astimezone(UTC).astimezone(CAMPUS_TIME_ZONE)


def validate_activity_range(starts_at: str, ends_at: str) -> None:
    start, end = (campus_datetime(value).replace(second=0, microsecond=0) for value in (starts_at, ends_at))
    if end <= start:
        raise ValueError("结束时间须晚于开始时间（精确到分钟）")

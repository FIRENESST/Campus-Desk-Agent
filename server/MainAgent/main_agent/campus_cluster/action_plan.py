"""The structured contract shared by MainAgent and campus service skills."""

from datetime import timedelta, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


ArgumentName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
ArgumentValue = Annotated[str, Field(min_length=1, max_length=1500)]
CAMPUS_ACTION_CONFIRMATION = "campus_action_confirmation"
CAMPUS_ACTION_DETAILS = "campus_action_details_required"
CAMPUS_TIME_ZONE = timezone(timedelta(hours=8))


class CampusActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True)

    action: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,23}\.[a-z][a-z0-9_]{0,31}$")]
    target: Annotated[str, Field(max_length=1500)] = ""
    arguments: Annotated[dict[ArgumentName, ArgumentValue], Field(max_length=8)] = Field(default_factory=dict)

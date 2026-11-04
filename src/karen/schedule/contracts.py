"""Validated schedule rules and persisted definitions."""

from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ScheduleRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["once", "daily", "weekly", "interval"]
    timezone: str
    at: datetime | None = None
    time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekday: int | None = Field(default=None, ge=0, le=6, strict=True)
    interval_seconds: int | None = Field(default=None, ge=60, le=31622400, strict=True)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("A valid IANA timezone is required") from None
        return value

    @model_validator(mode="after")
    def valid_rule(self):
        required = {
            "once": {"at"}, "daily": {"time"},
            "weekly": {"time", "weekday"}, "interval": {"interval_seconds"},
        }[self.kind]
        actual = {key for key in ("at", "time", "weekday", "interval_seconds")
                  if getattr(self, key) is not None}
        if actual != required:
            raise ValueError("Only the fields required by the schedule kind may be set")
        if self.at is not None and (self.at.tzinfo is None or self.at.utcoffset() is None):
            raise ValueError("One-time execution must have an explicit UTC offset")
        return self

    def next_after(self, now: datetime, *, previous: datetime | None = None):
        """Return a strictly future UTC occurrence, retaining interval alignment."""
        now = now.astimezone(UTC)
        if self.kind == "once":
            at = self.at.astimezone(UTC)
            return at if at > now else None
        if self.kind == "interval":
            interval = timedelta(seconds=self.interval_seconds)
            if previous is None:
                return now + interval
            previous = previous.astimezone(UTC)
            return previous + interval * max(1, (now - previous) // interval + 1)
        zone = ZoneInfo(self.timezone)
        local_date = now.astimezone(zone).date()
        for offset in range(15):
            date = local_date + timedelta(days=offset)
            if self.kind == "weekly" and date.weekday() != self.weekday:
                continue
            # First occurrence on a repeated clock hour; skip nonexistent local times.
            local = datetime.combine(date, time.fromisoformat(self.time), zone)
            candidate = local.astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                continue
            if candidate > now:
                return candidate
        raise ValueError("Cannot find the next calendar occurrence")


class ScheduleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "update", "delete", "run", "list", "clarify"]
    target: str | None = Field(default=None, min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    rule: ScheduleRule | None = None
    question: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "create" and not (self.name and self.prompt and self.rule):
            raise ValueError("Creating a schedule requires name, prompt and rule")
        if self.action in {"update", "delete", "run"} and not self.target:
            raise ValueError("A target is required")
        if self.action == "update" and not (self.name or self.prompt or self.rule):
            raise ValueError("An update must change something")
        if self.action == "clarify" and not self.question:
            raise ValueError("A clarification question is required")
        return self


class ScheduledTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    name: str
    prompt: str
    rule: ScheduleRule
    next_run: datetime | None
    last_task_id: str | None = None
    last_state: str = "not_run"
    last_skip: str | None = None

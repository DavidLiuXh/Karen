"""Public memory contracts and the structured model responses they validate."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def checked_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError("timezone must be a valid IANA timezone") from None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeRange(Contract):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def valid_range(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError("time range must contain ordered, timezone-aware instants")
        return self


class ContextEvent(Contract):
    schema_version: Literal[1] = 1
    event_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    conversation_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    event_type: Literal["user_message", "assistant_message", "goal_created", "task_result"]
    occurred_at: datetime = Field(default_factory=utcnow)
    timezone: str
    project_id: str | None = None
    redacted_fields: list[str] = Field(default_factory=list)
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def valid_clock(self):
        checked_zone(self.timezone)
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return self


class WriteReceipt(Contract):
    event_id: str
    sequence: int


class WriteStatus(Contract):
    receipt: WriteReceipt
    raw: Literal["queued", "persisted", "failed"] = "queued"
    derived: Literal["pending", "extracting", "committed", "skipped", "failed"] = "pending"
    index: Literal["pending", "indexed", "partial", "failed"] = "pending"
    attempts: int = 0
    error_code: str | None = None


class MemoryError(Exception):
    """Only safe codes escape the storage/model boundary."""


class MemoryQueueFull(MemoryError):
    pass


class PersistenceError(MemoryError):
    pass


class MemoryFlushError(MemoryError):
    def __init__(self, statuses: list[WriteStatus]):
        self.statuses = statuses
        super().__init__("MEMORY_FLUSH_INCOMPLETE")


class SourceRef(Contract):
    event_id: str
    pointer: str
    quote: str = ""
    source_role: Literal["user", "assistant", "tool"]
    occurred_at: datetime
    relative_file: str | None = None
    storage_state: Literal["queued", "persisted", "failed"] = "persisted"


class Evidence(Contract):
    event_id: str
    pointer: str
    quote: str = Field(min_length=1)


class Scope(Contract):
    kind: Literal["global", "project"] = "global"
    project_id: str | None = None

    @model_validator(mode="after")
    def valid_scope(self):
        if (self.kind == "project") != bool(self.project_id):
            raise ValueError("project scope requires project_id; global scope forbids it")
        return self


class TemporalValue(Contract):
    value: str
    precision: Literal["instant", "day", "month"]
    timezone: str
    origin: Literal["explicit", "inferred"] = "explicit"

    @model_validator(mode="after")
    def valid_value(self):
        checked_zone(self.timezone)
        fmt = {"day": "%Y-%m-%d", "month": "%Y-%m"}
        if self.precision == "instant":
            if datetime.fromisoformat(self.value).tzinfo is None:
                raise ValueError("instant requires an offset")
        else:
            pattern = (
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}" if self.precision == "day" else r"[0-9]{4}-[0-9]{2}"
            )
            if not re.fullmatch(pattern, self.value):
                raise ValueError("date must use canonical ISO precision")
            datetime.strptime(self.value, fmt[self.precision])
        return self

    def bounds(self) -> tuple[datetime, datetime]:
        if self.precision == "instant":
            instant = datetime.fromisoformat(self.value)
            return instant, instant
        start = datetime.fromisoformat(self.value + ("-01" if self.precision == "month" else ""))
        start = start.replace(tzinfo=ZoneInfo(self.timezone))
        if self.precision == "day":
            end = start + timedelta(days=1)
        else:
            end = (
                start.replace(year=start.year + 1, month=1)
                if start.month == 12
                else start.replace(month=start.month + 1)
            )
        return start, end


class FactCandidate(Contract):
    candidate_id: str
    subject: str = "user"
    fact_key: str = Field(min_length=1)
    value: JsonValue
    text: str = Field(min_length=1)
    scope: Scope = Field(default_factory=Scope)
    evidence: list[Evidence] = Field(min_length=1)
    valid_from: TemporalValue | None = None
    valid_to: TemporalValue | None = None

    @model_validator(mode="after")
    def valid_period(self):
        if (
            self.valid_from
            and self.valid_to
            and self.valid_from.bounds()[0] > self.valid_to.bounds()[1]
        ):
            raise ValueError("INVALID_FACT_PERIOD")
        return self


class SummaryDraft(Contract):
    text: str = Field(min_length=1)
    event_kind: str
    actions: list[dict[str, JsonValue]] = Field(default_factory=list)
    facts: list[dict[str, JsonValue]] = Field(default_factory=list)
    outcome: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_refs: list[dict[str, JsonValue]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)


class Extraction(Contract):
    facts: list[FactCandidate] = Field(default_factory=list)
    summaries: list[SummaryDraft] = Field(default_factory=list)


class FactDecision(Contract):
    candidate_id: str
    verification: Literal["supported", "uncertain", "rejected"]
    operation: Literal["new", "reinforce", "replace", "correct", "conflict", "ignore"]
    matched_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class Verification(Contract):
    decisions: list[FactDecision]


class StoredMemory(Contract):
    memory_id: str
    layer: Literal["m1", "m2"]
    text: str
    subject: str = "user"
    fact_key: str = ""
    value: JsonValue = None
    scope: Scope = Field(default_factory=Scope)
    sources: list[SourceRef]
    state: Literal["active", "superseded", "corrected", "conflicted"] = "active"
    verification_state: Literal["supported", "uncertain", "rejected"] = "supported"
    verification_reason: str = ""
    assertion_type: Literal["explicit", "observed", "inferred"] = "explicit"
    recorded_at: datetime
    created_at: datetime
    updated_at: datetime
    valid_from: TemporalValue | None = None
    valid_to: TemporalValue | None = None
    supersedes: list[str] = Field(default_factory=list)
    corrects: list[str] = Field(default_factory=list)
    conflict_group_id: str | None = None
    related_memory_ids: list[str] = Field(default_factory=list)
    request_id: str
    event_kind: str = ""
    actions: list[dict[str, JsonValue]] = Field(default_factory=list)
    facts: list[dict[str, JsonValue]] = Field(default_factory=list)
    outcome: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_refs: list[dict[str, JsonValue]] = Field(default_factory=list)
    extractor_model: str | None = None
    verifier_model: str | None = None
    prompt_version: str = "1"
    revision: int = 0


class RecallQuery(Contract):
    text: str = Field(min_length=1)
    timezone: str
    current_time_utc: datetime = Field(default_factory=utcnow)
    conversation_id: str
    request_id: str
    exclude_event_ids: list[str] = Field(default_factory=list)
    time_constraint: TimeRange | None = None
    project_id: str | None = None
    current_task_messages: list[dict[str, JsonValue]] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_clock(self):
        checked_zone(self.timezone)
        if self.current_time_utc.tzinfo is None:
            raise ValueError("current_time_utc must be timezone-aware")
        return self


class QueryAnalysis(Contract):
    search_text: str = Field(min_length=1)
    kind: Literal["relevance", "detail", "collection"] = "relevance"
    dialogue_dependency: Literal["none", "needed", "uncertain"] = "none"
    time_mode: Literal["current", "effective_at", "known_at", "timeline", "unspecified"] = "current"
    at: datetime | None = None
    time_range: TimeRange | None = None
    entities: list[str] = Field(default_factory=list)
    needed_fact_keys: list[str] = Field(default_factory=list)
    task_status: Literal["COMPLETED", "FAILED", "CANCELLED"] | None = None

    @model_validator(mode="after")
    def valid_at(self):
        if self.at is not None and self.at.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        if self.time_mode in {"effective_at", "known_at"} and self.at is None:
            raise ValueError("historical query requires at")
        return self


class RankedCandidate(Contract):
    memory_id: str
    relevance: Literal["relevant", "uncertain", "irrelevant"]
    reason: str


class Ranking(Contract):
    ranking: list[RankedCandidate]
    history_status: Literal["none", "selected", "ambiguous", "unavailable"] = "none"
    related_request_ids: list[str] = Field(default_factory=list)
    selected_event_ids: list[str] = Field(default_factory=list)
    history_reason: str = Field(default="", max_length=500)


class MemoryHit(Contract):
    memory: StoredMemory
    relevance: Literal["relevant", "uncertain", "unverified"]
    fusion_rank: int
    rerank_rank: int | None = None
    vector_score: float | None = None
    bm25_rank: int | None = None
    evidence_only: bool = False


class DetailQuery(Contract):
    text: str
    sources: list[SourceRef] = Field(default_factory=list)
    request_id: str | None = None
    conversation_id: str | None = None
    time_range: TimeRange | None = None


class DetailHit(Contract):
    text: str
    source: SourceRef
    truncated: bool = False


class DetailSearchResult(Contract):
    status: Literal["complete", "partial", "needs_scope", "unavailable"]
    hits: list[DetailHit] = Field(default_factory=list)
    scanned_events: int = 0
    scope: dict[str, JsonValue] = Field(default_factory=dict)


class History(Contract):
    status: Literal["none", "selected", "ambiguous", "unavailable"] = "none"
    reason: str = ""
    messages: list[dict[str, JsonValue]] = Field(default_factory=list)


class RecallResult(Contract):
    status: Literal["ok", "empty", "degraded", "unavailable"]
    m1: list[MemoryHit] = Field(default_factory=list)
    m2: list[MemoryHit] = Field(default_factory=list)
    details: list[DetailHit] = Field(default_factory=list)
    related_request_ids: list[str] = Field(default_factory=list)
    history: History = Field(default_factory=History)
    snapshot_revision: int = 0
    query_time_basis: dict[str, JsonValue] = Field(default_factory=dict)
    degradations: list[str] = Field(default_factory=list)
    coverage: dict[str, JsonValue] = Field(default_factory=dict)
    collection: dict[str, JsonValue] | None = None

    def context(self) -> dict[str, JsonValue]:
        """A bounded, sourced JSON package; never includes vectors or credentials."""
        return self.model_dump(mode="json")

"""Public client contracts, including owner-visible provider reasoning streams."""

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from domain.schemas import (
    ExpertName,
    ImageInput,
    Plan,
    Presentation,
    QuestionBlock,
    SkillName,
    StrictModel,
    Subject,
)
from domain.syllabus import SyllabusInfo

SessionStatus = Literal["queued", "running", "waiting", "completed", "failed", "cancelled"]


class SessionSummary(StrictModel):
    id: str
    title: str
    summary: str
    subject: Subject | None
    grade: str
    province: str | None
    textbook: str
    image_count: int
    status: SessionStatus
    expert: ExpertName | None
    created_at: str
    updated_at: str
    pinned: bool
    archived: bool


class SessionPage(StrictModel):
    items: list[SessionSummary]
    next_cursor: str | None


class SessionPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    pinned: bool | None = None
    archived: bool | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("Title cannot be blank")
        return value


class MessageRequest(StrictModel):
    message: str = Field(min_length=1, max_length=20000)
    images: list[ImageInput] = Field(default_factory=list, max_length=4)
    allow_paid_images: bool = False
    allow_max_thinking: bool = True

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message cannot be blank")
        return value


class PublicImage(StrictModel):
    index: int
    media_type: str
    bytes: int
    url: str


class PublicMessage(StrictModel):
    id: int
    session_id: str
    role: Literal["student", "assistant"]
    kind: Literal["question", "follow_up", "answer", "presentation"]
    text: str | None = None
    question_id: str | None = None
    images: list[PublicImage] = []
    presentation: Presentation | None = None
    created_at: str
    time_accuracy: Literal["exact", "inferred"] = "exact"


class MessagePage(StrictModel):
    items: list[PublicMessage]
    next_after: int | None


class Preferences(StrictModel):
    grade: Literal["高一", "高二", "高三"] = "高三"
    province: str | None = Field(default=None, max_length=30)
    textbook: str = Field(default="部编版/普通高中教材", min_length=1, max_length=100)
    subject: Subject | None = None


class Profile(StrictModel):
    id: int
    username: str
    created_at: str
    preferences: Preferences


class PasswordChange(StrictModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=32, max_length=200)


class AuthTokens(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


class ExpertInfo(StrictModel):
    name: ExpertName
    translation: str
    subjects: list[Subject]
    skills: list[SkillName]
    effort: Literal["off", "low", "high", "max"]
    allow_max_thinking: bool
    images: bool


class ExpertsResponse(StrictModel):
    experts: list[ExpertInfo]
    skills: dict[str, str]


class SkillCapability(StrictModel):
    name: SkillName
    availability: Literal["ready", "configured", "unavailable", "disabled"]
    reason: str | None = None


class ImagePrice(StrictModel):
    model: str
    credits: int
    cost: str
    currency: Literal["CNY"] = "CNY"
    requires_explicit_consent: bool


class StageModelInfo(StrictModel):
    provider: str
    model: str
    context_limit: int | None
    max_output_tokens: int
    effort: Literal["off", "low", "high", "max"]
    consumption_excluded: bool
    availability: Literal["configured", "unavailable"]


class Capabilities(StrictModel):
    version: str
    experts: list[ExpertInfo]
    skills: list[SkillCapability]
    image_max_count: int = 4
    image_max_bytes: int = 4 * 1024**2
    image_max_pixels: int = 4_000_000
    image_media_types: list[str] = ["image/png", "image/jpeg", "image/webp", "image/gif"]
    formats: list[str] = ["markdown", "latex", "svg", "html", "question", "branch", "localfile"]
    image_price: ImagePrice
    max_steps: int
    max_tool_calls: int
    max_output_tokens: int
    stage_max_output_tokens: dict[str, int]
    stage_efforts: dict[str, Literal["off", "low", "high", "max"]]
    stage_models: dict[str, StageModelInfo]
    expert_max_output_tokens: int
    streaming: bool = True
    reasoning_streaming: bool = True
    reasoning_source: Literal["provider_returned_thinking"] = "provider_returned_thinking"
    reasoning_format: Literal["text"] = "text"
    artifact_placement: Literal["ordered_blocks"] = "ordered_blocks"
    stream_preview_format: Literal["text"] = "text"
    planner_max_experts: list[ExpertName] = ["Euclid", "Turing", "Nomos"]
    continuation_states: list[SessionStatus] = ["completed", "failed", "cancelled"]
    budget_enforced: bool = True
    session_budget_cny: str = "5"
    pricing: "PricingInfo"
    syllabus: "SyllabusInfo | None" = None


class PricingInfo(StrictModel):
    mode: Literal["auto", "peak", "off_peak"]
    current_band: Literal["peak", "off_peak"]
    timezone: Literal["UTC+08:00"] = "UTC+08:00"
    peak_weekdays: list[int]
    peak_windows: list[tuple[str, str]]
    off_peak_multiplier: str
    holiday_calendar_years: list[int]
    holiday_calendar_complete: bool
    version: str
    settlement_clock: Literal["request_start"] = "request_start"
    reservation_band: Literal["peak"] = "peak"


class BudgetView(StrictModel):
    session_id: str
    currency: Literal["CNY"]
    limit: str
    spent: str
    uncertain: str
    reserved: str
    available: str
    exhausted: bool


class EmptyData(StrictModel):
    pass


class CodeData(StrictModel):
    code: str


class WarningData(CodeData):
    fallback: Literal["markdown"]


class WaitingData(StrictModel):
    question: QuestionBlock


class ToolData(StrictModel):
    name: str
    result: dict[str, Any]


class MessageData(StrictModel):
    message: PublicMessage


class EmptyEvent(StrictModel):
    id: int
    event: Literal["queued", "running", "completed"]
    data: EmptyData
    created_at: str


class PlanEvent(StrictModel):
    id: int
    event: Literal["plan"]
    data: Plan
    created_at: str


class PresentationEvent(StrictModel):
    id: int
    event: Literal["presentation"]
    data: Presentation
    created_at: str


class ToolEvent(StrictModel):
    id: int
    event: Literal["tool"]
    data: ToolData
    created_at: str


class WaitingEvent(StrictModel):
    id: int
    event: Literal["waiting"]
    data: WaitingData
    created_at: str


class TerminalEvent(StrictModel):
    id: int
    event: Literal["failed", "cancelled"]
    data: CodeData
    created_at: str


class WarningEvent(StrictModel):
    id: int
    event: Literal["warning"]
    data: WarningData
    created_at: str


class MessageEvent(StrictModel):
    id: int
    event: Literal["message"]
    data: MessageData
    created_at: str


class OutputStartData(StrictModel):
    stream_id: str
    round: int
    effort: Literal["off", "low", "high", "max"]


class OutputDeltaData(StrictModel):
    stream_id: str
    sequence: int
    text: str


class OutputEndData(StrictModel):
    stream_id: str
    status: Literal["received", "aborted"]


class ThinkingData(StrictModel):
    effort: Literal["max"]
    source: Literal["planner", "expert"]
    reason: str


class OutputStartEvent(StrictModel):
    id: int
    event: Literal["output_start"]
    data: OutputStartData
    created_at: str


class OutputDeltaEvent(StrictModel):
    id: int
    event: Literal["output_delta"]
    data: OutputDeltaData
    created_at: str


class OutputEndEvent(StrictModel):
    id: int
    event: Literal["output_end"]
    data: OutputEndData
    created_at: str


class ThinkingEvent(StrictModel):
    id: int
    event: Literal["thinking"]
    data: ThinkingData
    created_at: str


class ReasoningStartEvent(StrictModel):
    id: int
    event: Literal["reasoning_start"]
    data: OutputStartData
    created_at: str


class ReasoningDeltaEvent(StrictModel):
    id: int
    event: Literal["reasoning_delta"]
    data: OutputDeltaData
    created_at: str


class ReasoningEndEvent(StrictModel):
    id: int
    event: Literal["reasoning_end"]
    data: OutputEndData
    created_at: str


class ArtifactData(StrictModel):
    round: int
    status: Literal["received", "aborted"]
    artifact_id: str
    name: str
    mime: str
    bytes: int


class ArtifactEvent(StrictModel):
    id: int
    event: Literal["artifact"]
    data: ArtifactData
    created_at: str


PublicEvent = Annotated[
    EmptyEvent
    | PlanEvent
    | PresentationEvent
    | ToolEvent
    | WaitingEvent
    | TerminalEvent
    | WarningEvent
    | MessageEvent
    | OutputStartEvent
    | OutputDeltaEvent
    | OutputEndEvent
    | ThinkingEvent
    | ReasoningStartEvent
    | ReasoningDeltaEvent
    | ReasoningEndEvent
    | ArtifactEvent,
    Field(discriminator="event"),
]


class EventPage(StrictModel):
    items: list[PublicEvent]
    next_after: int | None


class UsageGroup(StrictModel):
    key: str
    currency: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    estimated_cost: str
    unknown_cost_calls: int


class UsageSummary(StrictModel):
    group_by: Literal["day", "session", "total"]
    date_from: str
    date_to: str
    items: list[UsageGroup]
    next_cursor: str | None


class ErrorInfo(StrictModel):
    code: str
    status: int
    recovery: str
    zh: str
    en: str


class ErrorCatalog(StrictModel):
    items: list[ErrorInfo]

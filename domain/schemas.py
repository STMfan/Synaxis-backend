import base64
import io
from typing import Annotated, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Subject = Literal[
    "chinese", "english", "math", "physics", "chemistry", "biology", "politics", "history", "geography"
]
ExpertName = Literal["Poiesis", "Nexus", "Euclid", "Turing", "Nomos", "Panopt", "Chronos", "Meridian"]
SkillName = Literal["Search", "AssistantOnWeb", "Syllabus", "GPTImage", "PythonBox", "Render"]


class ImageInput(StrictModel):
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data: str = Field(max_length=6_000_000, description="Base64 image / Base64 编码图片，单张最多 4 MiB")

    @field_validator("data")
    @classmethod
    def valid_base64(cls, v):
        try:
            raw = base64.b64decode(v, validate=True)
        except ValueError as exc:
            raise ValueError("Invalid base64 image") from exc
        if not raw or len(raw) > 4 * 1024**2:
            raise ValueError("Image must contain 1 byte–4 MiB")
        return v

    @model_validator(mode="after")
    def valid_image(self):
        try:
            with Image.open(io.BytesIO(base64.b64decode(self.data))) as image:
                expected = {
                    "image/png": "PNG",
                    "image/jpeg": "JPEG",
                    "image/webp": "WEBP",
                    "image/gif": "GIF",
                }
                if image.format != expected[self.media_type] or image.width * image.height > 4_000_000:
                    raise ValueError("Image format mismatch or more than 4 million pixels")
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("Invalid image") from exc
        return self


class TutorRequest(StrictModel):
    message: str = Field(min_length=1, max_length=20000, description="Question / 题目或知识问题")
    subject: Subject | None = Field(default=None, description="Optional routing hint / 可选科目提示")
    grade: Literal["高一", "高二", "高三"] = "高三"
    province: str | None = Field(default=None, max_length=30, description="Exam region / 高考省份")
    textbook: str = Field(default="部编版/普通高中教材", max_length=100)
    images: list[ImageInput] = Field(default_factory=list, max_length=4)
    allow_paid_images: bool = Field(
        default=False, description="Explicitly permit IMAGE-CREDIT > 0 / 授权付费绘图"
    )
    allow_max_thinking: bool = Field(
        default=True, description="Allow rare Max escalation / 允许复杂问题按需升级 Max"
    )

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message cannot be blank")
        return value


class Plan(StrictModel):
    subject: Subject
    expert: ExpertName
    problem_type: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=1, max_length=20000)
    skills: list[SkillName] = Field(default_factory=list, max_length=6)
    rationale: str = Field(default="", max_length=500)
    thinking: Literal["default", "max"] = "default"
    complexity: Literal["simple", "normal", "hard", "extreme"] = "normal"
    max_reason: str = Field(default="", max_length=300)


class MarkdownBlock(StrictModel):
    type: Literal["markdown"]
    text: str = Field(max_length=2_000_000)


class LatexBlock(StrictModel):
    type: Literal["latex"]
    source: str = Field(max_length=20000, description="MathJax v4 TeX / MathJax v4 公式")
    display: bool = True


class SvgBlock(StrictModel):
    type: Literal["svg"]
    source: str = Field(max_length=100000)


class HtmlBlock(StrictModel):
    type: Literal["html"]
    source: str = Field(
        max_length=100000, description="Static card, CSS/details interactions / 静态卡片与 CSS/details 交互"
    )


class QuestionBlock(StrictModel):
    type: Literal["question"]
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    prompt: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=10)


class FileBlock(StrictModel):
    type: Literal["localfile"]
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$", description="Authorized artifact ID / 文件授权 ID")
    label: str = Field(min_length=1, max_length=100)


LeafBlock = Annotated[
    MarkdownBlock | LatexBlock | SvgBlock | HtmlBlock | QuestionBlock | FileBlock, Field(discriminator="type")
]


class Tab(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    label: str = Field(min_length=1, max_length=50)
    blocks: list[LeafBlock] = Field(min_length=1, max_length=30)


class BranchBlock(StrictModel):
    type: Literal["branch"]
    selected: str
    tabs: list[Tab] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def check_tabs(self):
        ids = [t.id for t in self.tabs]
        if len(set(ids)) != len(ids) or self.selected not in ids:
            raise ValueError("Tab IDs must be unique and selected must exist")
        return self


Block = Annotated[
    MarkdownBlock | LatexBlock | SvgBlock | HtmlBlock | QuestionBlock | FileBlock | BranchBlock,
    Field(discriminator="type"),
]


class Presentation(StrictModel):
    version: Literal["1.0"] = "1.0"
    blocks: list[Block] = Field(min_length=1, max_length=50)

    def leaves(self):
        for block in self.blocks:
            if isinstance(block, BranchBlock):
                for tab in block.tabs:
                    yield from tab.blocks
            else:
                yield block

    @model_validator(mode="after")
    def unique_questions(self):
        ids = [b.id for b in self.leaves() if isinstance(b, QuestionBlock)]
        if len(ids) != len(set(ids)) or len(ids) > 1:
            raise ValueError("At most one question per round, with a unique ID")
        return self


class AnswerRequest(StrictModel):
    question_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    answer: str = Field(min_length=1, max_length=10000)


class TokenResponse(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class SessionView(StrictModel):
    id: str
    status: Literal["queued", "running", "waiting", "completed", "failed", "cancelled"]
    expert: ExpertName | None = None
    question: QuestionBlock | None = None
    presentations: list[Presentation] = []
    error: str | None = None
    title: str = ""
    summary: str = ""
    subject: Subject | None = None
    grade: str = "高三"
    province: str | None = None
    textbook: str = "部编版/普通高中教材"
    image_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    pinned: bool = False
    archived: bool = False
    original_message: str = ""
    submission_message_id: int | None = None


class APIError(StrictModel):
    code: str
    detail: str


class UsageRecord(StrictModel):
    id: int
    session_id: str | None
    stage: str
    model: str
    status: str
    created_at: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost: str | None
    currency: str
    pricing_basis: str
    error: str | None


class UsagePage(StrictModel):
    items: list[UsageRecord]
    next_before: int | None

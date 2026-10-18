"""Public, traceable curriculum retrieval contracts."""

from typing import Any, Literal

from pydantic import Field, field_validator

from domain.schemas import StrictModel, Subject

CourseCategory = Literal["必修课程", "选择性必修课程"]
RetrievalMode = Literal["lexical", "semantic", "hybrid"]
SUBJECTS = {
    "chinese": "语文",
    "english": "英语",
    "math": "数学",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物学",
    "politics": "思想政治",
    "history": "历史",
    "geography": "地理",
}


class SyllabusQuery(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    subject: Subject | None = None
    course_category: CourseCategory | None = None
    top_k: int | None = Field(default=None, ge=1, le=10)
    mode: RetrievalMode | None = None

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Query cannot be blank")
        return value.strip()


class SyllabusSource(StrictModel):
    file: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_pages: list[int]
    pdf_page_start: int
    pdf_page_end: int
    printed_pages: list[int]


class SyllabusChunk(StrictModel):
    schema_version: Literal["1.0"]
    id: str
    section_id: str
    document_id: str
    subject: str
    course_categories: list[CourseCategory]
    original_course_categories: list[str]
    scope: str
    heading_path: list[str]
    text: str
    context_before: str
    context_pdf_pages: list[int]
    embedding_text: str
    source: SyllabusSource
    quality_warnings: list[str]


class SyllabusHit(SyllabusChunk):
    score: float
    retrieval_method: RetrievalMode


class SyllabusSection(StrictModel):
    schema_version: Literal["1.0"]
    id: str
    document_id: str
    subject: str
    course_categories: list[CourseCategory]
    original_course_categories: list[str]
    scope: str
    title: str
    heading_path: list[str]
    text: str
    fragments: list[dict[str, Any]]
    source: SyllabusSource
    quality_warnings: list[str]
    classification_evidence: dict[str, Any] | None = None
    extraction_method: str | None = None
    table_row: int | None = None
    table_caption: str | None = None


class SyllabusInfo(StrictModel):
    availability: Literal["ready", "unavailable", "disabled"]
    reason: str | None = None
    edition: str | None = None
    subjects: list[Subject] = []
    course_categories: list[CourseCategory] = ["必修课程", "选择性必修课程"]
    modes: list[RetrievalMode] = []
    default_mode: RetrievalMode = "hybrid"
    model: str | None = None
    chunks: int = 0
    sections: int = 0
    local_only: bool = True
    query_token_limit: int = 512
    scope: str = "指定 2017 年版课标的必修、选择性必修及适用共用课程内容；不含独立的高考考纲或各省细则。"


class SyllabusResult(StrictModel):
    status: Literal["ok", "unavailable", "disabled"]
    code: str | None = None
    subject: Subject | None = None
    edition: str | None = None
    mode: RetrievalMode
    count: int = 0
    results: list[SyllabusHit] = []
    message: str | None = None

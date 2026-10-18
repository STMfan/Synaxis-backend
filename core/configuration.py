"""Validated, restart-to-reload JSON configuration. No credentials live here."""

import json
import os
from datetime import date, time
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from domain.schemas import ExpertName, SkillName, StrictModel, Subject

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = Path(os.environ.get("SYNAXIS_CONFIG_DIR", str(ROOT / "config")))
CONFIG_DIR = CONFIG_ROOT / "ExpertsPrompts"
Effort = Literal["off", "low", "high", "max"]


class ModelRole(StrictModel):
    env: str
    model: str = Field(min_length=1)
    effort: Effort


class ProcessorConfig(StrictModel):
    env: str = "SILICONFLOW-MODELID-PROCESS"
    model: str = Field(default="Qwen/Qwen3.5-9B", min_length=1)
    base: str = "https://api.siliconflow.cn/v1"
    effort: Effort = "off"
    output_limits: dict[str, int] = Field(default_factory=dict, exclude=True)
    context_limits: dict[str, int] = Field(default_factory=dict, exclude=True)
    # User explicitly excludes voucher-covered formatter calls from consumption.
    excluded_from_budget: Literal[True] = True

    @field_validator("output_limits", "context_limits")
    @classmethod
    def valid_limits(cls, value):
        if any(type(n) is not int or n < 256 or n > 1_048_576 for n in value.values()):
            raise ValueError("Processor limits must be in 256..1048576")
        return value


class ModelConfig(StrictModel):
    main: ModelRole
    literature: ModelRole
    processor: ProcessorConfig
    deepseek_base: str
    max_tokens: int = Field(ge=256, le=393216)
    expert_max_tokens: int = Field(default=393216, ge=256, le=393216)
    model_output_limits: dict[str, int] = {}
    model_context_limits: dict[str, int] = {}
    image_model: str
    price_rates_cny: dict[str, list[float]]
    price_rates: dict[str, list[float]]

    @field_validator("model_output_limits", "model_context_limits")
    @classmethod
    def positive_limits(cls, value):
        if any(limit < 256 or limit > 1_048_576 for limit in value.values()):
            raise ValueError("Model limits must be in 256..1048576")
        return value


class SharedConfig(StrictModel):
    common: str = Field(min_length=1)
    base_skills: str
    response_style: str
    output_contract: str
    presentation_contract: str = Field(min_length=1)
    visual_contract: str = ""
    max_thinking: str


class Expert(StrictModel):
    name: ExpertName
    translation: str
    subjects: tuple[Subject, ...]
    instructions: str = Field(min_length=1)
    exskills: tuple[SkillName, ...]
    model_role: Literal["main", "literature"]
    effort: Effort | None = None
    images: bool = True
    allow_max_thinking: bool = True

    @model_validator(mode="after")
    def text_only_poiesis(self):
        if self.name == "Poiesis" and self.images:
            raise ValueError("Poiesis cannot accept images")
        return self


class Stage(StrictModel):
    # Accepted for old JSON migration only; routing now belongs to the role registry.
    provider: str | None = Field(default=None, exclude=True)
    model_role: Literal["main", "literature", "process"]
    effort: Effort | None = None
    instructions: str = Field(min_length=1)
    output_contract: str = ""
    max_tokens: int | None = Field(default=None, ge=256, le=393216)
    max_experts: list[Literal["Euclid", "Turing", "Nomos"]] = []

class ToolsConfig(StrictModel):
    help: dict[str, str]
    descriptions: dict[str, str]


class PricingConfig(StrictModel):
    version: str
    source: str
    holiday_source: str
    peak_weekdays: list[int]
    peak_windows: list[tuple[str, str]]
    off_peak_multiplier: float = Field(gt=0, le=1)
    holiday_calendar_years: list[int]
    holiday_ranges: list[tuple[date, date]]

    @model_validator(mode="after")
    def valid_calendar(self):
        if not self.peak_weekdays or any(type(n) is not int or not 0 <= n <= 6 for n in self.peak_weekdays):
            raise ValueError("Weekdays must be 0..6, Monday=0")
        ends = []
        for start, end in self.peak_windows:
            if time.fromisoformat(start) >= time.fromisoformat(end):
                raise ValueError("Peak windows cannot wrap midnight")
            if ends and time.fromisoformat(start) < ends[-1]:
                raise ValueError("Peak windows must be sorted and disjoint")
            ends.append(time.fromisoformat(end))
        if not ends or any(
            start > end or start.year != end.year or start.year not in self.holiday_calendar_years
            for start, end in self.holiday_ranges
        ):
            raise ValueError("Invalid holiday ranges or missing calendar year")
        return self


def load_config(name, schema, directory=CONFIG_DIR):
    # Errors deliberately fail startup, before any paid call is possible.
    return schema.model_validate_json((directory / f"{name}.json").read_text(encoding="utf-8"))


MODELS = load_config("Models", ModelConfig, CONFIG_ROOT / "models")
SHARED = load_config("Shared", SharedConfig)
EXPERTS = {
    name: load_config(name, Expert)
    for name in ("Poiesis", "Nexus", "Euclid", "Turing", "Nomos", "Panopt", "Chronos", "Meridian")
}
if any(name != expert.name for name, expert in EXPERTS.items()):
    raise ValueError("Expert filename must match its name")
STAGES = {name: load_config(name, Stage) for name in ("Planner", "Formatter", "AssistantOnWeb", "Search")}
for registry in (EXPERTS, STAGES):
    for name, config in registry.items():
        if config.effort is None:
            role = "processor" if config.model_role == "process" else config.model_role
            registry[name] = config.model_copy(update={"effort": getattr(MODELS, role).effort})
TOOLS = load_config("Tools", ToolsConfig)
PRICING = load_config("Pricing", PricingConfig, CONFIG_ROOT / "models")


def schema_json(schema):
    return json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))

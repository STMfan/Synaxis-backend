from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.configuration import MODELS
from core.runtime_config import NetworkConfig, config_directory, runtime_values
from llm.providers import TextProvider

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", populate_by_name=True)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        def json_settings():
            return runtime_values(init_settings().get("config_dir"))

        return init_settings, env_settings, dotenv_settings, json_settings, file_secret_settings

    config_dir: Path = Field(default_factory=config_directory)
    providers: dict[str, TextProvider] = {}
    host: str = "127.0.0.1"
    port: int = Field(default=5667, ge=1, le=65535)
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    admin_enabled: bool = True
    admin_allow_lan: bool = True
    admin_session_minutes: int = Field(default=30, ge=5, le=120)
    admin_password_hash: SecretStr = SecretStr("")
    main_provider: str = Field(default="deepseek", validation_alias="TEXT_MAIN_PROVIDER")
    literature_provider: str = Field(default="deepseek", validation_alias="TEXT_LITERATURE_PROVIDER")
    process_provider: str = Field(default="siliconflow", validation_alias="TEXT_PROCESS_PROVIDER")
    text_main_key: SecretStr = Field(default=SecretStr(""), validation_alias="TEXT_MAIN_API_KEY")
    text_literature_key: SecretStr = Field(default=SecretStr(""), validation_alias="TEXT_LITERATURE_API_KEY")
    text_process_key: SecretStr = Field(default=SecretStr(""), validation_alias="TEXT_PROCESS_API_KEY")

    main_model: str = Field(
        default=MODELS.main.model, validation_alias=AliasChoices("TEXT_MAIN_MODEL", MODELS.main.env)
    )
    literature_model: str = Field(
        default=MODELS.literature.model,
        validation_alias=AliasChoices("TEXT_LITERATURE_MODEL", MODELS.literature.env),
    )
    deepseek_key: SecretStr = Field(default=SecretStr(""), validation_alias="DEEPSEEK-APIKEY")
    text_search_key: SecretStr = Field(default=SecretStr(""), validation_alias="TEXT_SEARCH_API_KEY")
    deepseek_base: str = MODELS.deepseek_base
    process_model: str = Field(
        default=MODELS.processor.model,
        validation_alias=AliasChoices("TEXT_PROCESS_MODEL", MODELS.processor.env),
    )
    search_model: str = Field(default=MODELS.main.model, validation_alias="TEXT_SEARCH_MODEL")
    siliconflow_key: SecretStr = Field(default=SecretStr(""), validation_alias="SILICONFLOW-APIKEY")
    siliconflow_base: str = Field(default=MODELS.processor.base, validation_alias="SILICONFLOW-API")
    image_base: str = Field(default="", validation_alias="IMAGE-API")
    image_key: SecretStr = Field(default=SecretStr(""), validation_alias="IMAGE-KEY")
    image_model: str = Field(default=MODELS.image_model, validation_alias="IMAGE-MODELID")
    image_credit: int = Field(default=0, ge=0, validation_alias="IMAGE-CREDIT")
    data_dir: Path = ROOT / "data"
    jwt_secret: SecretStr = SecretStr("")
    jwt_minutes: int = Field(default=60, ge=1, le=1440)
    refresh_days: int = Field(default=30, ge=1, le=90)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    session_budget_cny: float = Field(default=5, gt=0, le=5)
    # Official mainland CNY peak prices, not a USD exchange-rate conversion.
    price_rates_cny: dict[str, list[float]] = MODELS.price_rates_cny
    max_tokens: int = Field(default=MODELS.max_tokens, ge=256, le=1048576)
    expert_max_tokens: int = Field(default=MODELS.expert_max_tokens, ge=256, le=1048576)
    max_steps: int = Field(default=8, ge=1, le=30)
    max_assistant_steps: int = Field(default=4, ge=1, le=8)
    max_tool_calls: int = Field(default=24, ge=1, le=100)
    request_timeout: float = Field(default=180, gt=0, le=600)
    session_timeout: float = Field(default=900, gt=0)
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    retention_days: int = Field(default=365, ge=1)
    artifact_retention_days: int = Field(default=365, ge=1)
    log_retention_days: int = Field(default=365, ge=1)
    retention_bytes: int = Field(default=2 * 1024**3, ge=65536)
    maintenance_seconds: int = Field(default=3600, ge=60)
    artifact_bytes: int = Field(default=8 * 1024**2, ge=1024)
    render_enabled: bool = True
    browser_path: str | None = None
    nsjail_root: Path | None = None
    nsjail_binary: str = "nsjail"
    sandbox_enabled: bool = True
    sandbox_concurrency: int = Field(default=2, ge=1, le=8)
    sandbox_seconds: int = Field(default=10, ge=1, le=30)
    pricing_mode: Literal["auto", "peak", "off_peak"] = "auto"
    syllabus_enabled: bool = True
    syllabus_data_dir: Path | None = None
    syllabus_model_cache: Path | None = None
    syllabus_mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    syllabus_top_k: int = Field(default=5, ge=1, le=10)
    syllabus_threads: int = Field(default=2, ge=1, le=8)
    # USD per million tokens. Versioned estimate; never presented as provider invoice.
    price_rates: dict[str, list[float]] = MODELS.price_rates

    @field_validator("price_rates", "price_rates_cny")
    @classmethod
    def rates_valid(cls, value):
        import math

        if any(len(v) != 3 or any(not math.isfinite(x) or x < 0 for x in v) for v in value.values()):
            raise ValueError("Rates need three finite nonnegative numbers: cache-hit, cache-miss, output")
        return value

    @field_validator("cors_origins")
    @classmethod
    def cors_valid(cls, value):
        return NetworkConfig.origins(value)

    @field_validator("host")
    @classmethod
    def host_valid(cls, value):
        return NetworkConfig.listen_address(value)

    @field_validator("data_dir", "nsjail_root", "syllabus_data_dir", "syllabus_model_cache")
    @classmethod
    def resolve_runtime_path(cls, value):
        return (ROOT / value).resolve() if value is not None and not value.is_absolute() else value

    @model_validator(mode="after")
    def registered_roles(self):
        if any(
            getattr(self, role + "_provider") not in self.providers
            for role in ("main", "literature", "process")
        ):
            raise ValueError("Text role names an unregistered provider")
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "synaxis.sqlite3"

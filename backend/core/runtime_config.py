"""Typed non-secret configuration; environment variables remain override-compatible."""

import ipaddress
import json
import os
import ssl
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from domain.schemas import StrictModel

CONFIG_ROOT = Path(__file__).resolve().parents[1] / "config"


class NetworkConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=5667, ge=1, le=65535)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    admin_enabled: bool = True
    admin_allow_lan: bool = True
    admin_session_minutes: int = Field(default=30, ge=5, le=120)

    @model_validator(mode="after")
    def tls_pair(self):
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValueError("TLS certificate and private key must be configured together")
        return self

    @field_validator("host")
    @classmethod
    def listen_address(cls, value):
        if value != "localhost":
            ipaddress.ip_address(value)
        return value

    @field_validator("cors_origins")
    @classmethod
    def origins(cls, value):
        for origin in value:
            parts = urlsplit(origin)
            if (
                parts.scheme not in ("http", "https")
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.path
                or parts.query
                or parts.fragment
                or "*" in origin
            ):
                raise ValueError("CORS entries must be exact http(s) origins, without paths or wildcards")
            if parts.port is not None and not 1 <= parts.port <= 65535:
                raise ValueError("Invalid origin port")
        return list(dict.fromkeys(value))


class StorageConfig(StrictModel):
    data_dir: str | None = None
    retention_days: int = Field(default=365, ge=1)
    retention_bytes: int = Field(default=2 * 1024**3, ge=65536)
    artifact_retention_days: int = Field(default=365, ge=1)
    log_retention_days: int = Field(default=365, ge=1)
    artifact_bytes: int = Field(default=8 * 1024**2, ge=1024)
    maintenance_seconds: int = Field(default=3600, ge=60)


class ExecutionConfig(StrictModel):
    max_steps: int = Field(default=8, ge=1, le=30)
    max_assistant_steps: int = Field(default=4, ge=1, le=8)
    max_tool_calls: int = Field(default=24, ge=1, le=100)
    request_timeout: float = Field(default=180, gt=0, le=600)
    session_timeout: float = Field(default=900, gt=0)
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    max_tokens: int = Field(default=393216, ge=256, le=1048576)
    expert_max_tokens: int = Field(default=393216, ge=256, le=1048576)
    session_budget_cny: float = Field(default=5, gt=0, le=5)
    jwt_minutes: int = Field(default=60, ge=1, le=1440)
    refresh_days: int = Field(default=30, ge=1, le=90)
    pricing_mode: str = "auto"


class SandboxConfig(StrictModel):
    sandbox_enabled: bool = True
    nsjail_root: str | None = None
    nsjail_binary: str = "nsjail"
    sandbox_seconds: int = Field(default=10, ge=1, le=30)
    sandbox_concurrency: int = Field(default=2, ge=1, le=8)


class RenderConfig(StrictModel):
    render_enabled: bool = True
    browser_path: str | None = None


class SyllabusConfig(StrictModel):
    syllabus_enabled: bool = True
    syllabus_data_dir: str | None = None
    syllabus_model_cache: str | None = None
    syllabus_mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    syllabus_top_k: int = Field(default=5, ge=1, le=10)
    syllabus_threads: int = Field(default=2, ge=1, le=8)


GROUPS = {
    "network": NetworkConfig,
    "storage": StorageConfig,
    "execution": ExecutionConfig,
    "sandbox": SandboxConfig,
    "render": RenderConfig,
    "syllabus": SyllabusConfig,
}


def config_directory():
    return Path(os.environ.get("SYNAXIS_CONFIG_DIR", str(CONFIG_ROOT))).resolve()


def runtime_values(directory=None):
    directory = Path(directory) if directory else config_directory()
    result = {}
    for name, schema in GROUPS.items():
        path = directory / "runtime" / f"{name}.json"
        group = schema.model_validate_json(path.read_text(encoding="utf-8"))
        result.update(group.model_dump())
    if result.get("data_dir") is None:
        result.pop("data_dir", None)
    from llm.providers import ProviderRegistry

    registry = ProviderRegistry.model_validate_json(
        (directory / "models/providers.json").read_text(encoding="utf-8")
    )
    result["providers"] = registry.providers
    result.update({role + "_provider": name for role, name in registry.roles.items()})
    return result


def save_network(directory, value):
    """Atomic validated replacement, no credentials accepted by this schema."""
    value = NetworkConfig.model_validate(value)
    path = Path(directory) / "runtime/network.json"
    if value.tls_certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(value.tls_certfile, value.tls_keyfile)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".network-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value.model_dump(), ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return value

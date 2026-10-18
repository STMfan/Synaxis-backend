"""Public provider capabilities and model limits. Credentials are role-scoped secrets."""

import math
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator

from domain.schemas import StrictModel


class TextProvider(StrictModel):
    protocol: Literal["deepseek_anthropic", "openai_chat"]
    base_url: str
    output_limits: dict[str, int] = {}
    context_limits: dict[str, int] = {}
    price_rates_cny: dict[str, list[float]] = {}
    excluded_from_budget: bool = False
    supports_images: bool = True
    supports_tools: bool = True
    supports_json_mode: bool = False
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_parameter: Literal["none", "reasoning_effort", "enable_thinking"] = "none"
    reasoning_values: dict[str, str | bool] = {}
    send_reasoning_history: bool = False
    stream_usage: bool = True

    @field_validator("base_url")
    @classmethod
    def safe_endpoint(cls, value):
        url = urlsplit(value)
        if (
            url.scheme not in ("https", "http")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Provider base_url must be an http(s) endpoint without credentials/query")
        return value.rstrip("/")

    @field_validator("output_limits", "context_limits")
    @classmethod
    def limits(cls, value):
        if any(type(v) is not int or not 256 <= v <= 1048576 for v in value.values()):
            raise ValueError("Model limits must be in 256..1048576")
        return value

    @field_validator("price_rates_cny")
    @classmethod
    def rates(cls, value):
        if any(len(v) != 3 or any(not math.isfinite(x) or x < 0 for x in v) for v in value.values()):
            raise ValueError("Rates: cache-hit/cache-miss/output CNY per million tokens")
        return value

    @model_validator(mode="after")
    def compatible_reasoning(self):
        if set(self.reasoning_values) - {"off", "low", "high", "max"}:
            raise ValueError("Unknown internal effort")
        if self.reasoning_parameter == "enable_thinking" and any(
            type(v) is not bool for v in self.reasoning_values.values()
        ):
            raise ValueError("enable_thinking requires boolean values")
        if self.reasoning_parameter == "reasoning_effort" and any(
            not isinstance(v, str) for v in self.reasoning_values.values()
        ):
            raise ValueError("reasoning_effort requires string values")
        return self


class ProviderRegistry(StrictModel):
    roles: dict[Literal["main", "literature", "process"], str]
    providers: dict[str, TextProvider]

    @model_validator(mode="after")
    def roles_exist(self):
        if (
            set(self.roles) != {"main", "literature", "process"}
            or not set(self.roles.values()) <= self.providers.keys()
        ):
            raise ValueError("Every text model role must name a registered provider")
        return self


def provider_for(settings, role):
    name = getattr(settings, role + "_provider")
    if name not in settings.providers:
        from domain.errors import HarnessError

        raise HarnessError("PROVIDER_NOT_CONFIGURED")
    return name, settings.providers[name]


def key_for(settings, role):
    key = getattr(settings, "text_" + role + "_key").get_secret_value()
    if key:
        return key
    name = getattr(settings, role + "_provider")
    # Migration only: never forward one supplier's credential to a custom endpoint.
    if name == "deepseek":
        return settings.deepseek_key.get_secret_value()
    if name == "siliconflow":
        return settings.siliconflow_key.get_secret_value()
    return ""


def supports_max(settings, role):
    _, provider = provider_for(settings, role)
    return provider.protocol == "deepseek_anthropic" or (
        provider.reasoning_parameter != "none" and "max" in provider.reasoning_values
    )

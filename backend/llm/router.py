"""Role-based text routing; native web search keeps its dedicated adapter."""

from pydantic import SecretStr

from core.configuration import STAGES
from llm.deepseek import DeepSeekClient
from llm.openai_compatible import OpenAICompatibleClient
from llm.providers import key_for, provider_for
from llm.siliconflow import SiliconFlowClient


class RoutedClient:
    def __init__(self, settings, db, http):
        self.settings = settings
        search_key = settings.text_search_key.get_secret_value() or (
            key_for(settings, "main")
            if settings.main_provider == "deepseek"
            else settings.deepseek_key.get_secret_value()
        )
        self.deepseek = DeepSeekClient(
            settings.model_copy(update={"deepseek_key": SecretStr(search_key)}), db, http
        )

    async def complete(self, ctx, **kwargs):
        if ctx.stage == "search":
            return await self.deepseek.complete(ctx, **kwargs)
        role = ctx.model_role or (
            "process"
            if ctx.stage == "formatter"
            else STAGES["AssistantOnWeb"].model_role
            if ctx.stage == "assistant"
            else STAGES["Planner"].model_role
            if ctx.stage == "planner"
            else "main"
        )
        settings = self.settings
        name, provider = provider_for(settings, role)
        if provider.protocol == "deepseek_anthropic":
            adapted = settings.model_copy(
                update={
                    "deepseek_key": SecretStr(key_for(settings, role)),
                    "deepseek_base": provider.base_url,
                }
            )
            client = DeepSeekClient(adapted, self.deepseek.db, self.deepseek.http)
        elif name == "siliconflow" and ctx.stage == "formatter" and role == "process" and provider.excluded_from_budget:
            adapted = settings.model_copy(
                update={
                    "siliconflow_key": SecretStr(key_for(settings, role)),
                    "siliconflow_base": provider.base_url,
                }
            )
            client = SiliconFlowClient(adapted, self.deepseek.db, self.deepseek.http)
        else:
            client = OpenAICompatibleClient(settings, self.deepseek.db, self.deepseek.http, role)
        return await client.complete(ctx, **kwargs)

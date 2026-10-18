"""Stable shared prefixes, then role/tool specialization and dynamic input."""

from core.configuration import EXPERTS, SHARED, STAGES, TOOLS
from domain.schemas import ExpertName

COMMON = SHARED.common
BASE_SKILLS = SHARED.base_skills
EXSKILL_HELP = TOOLS.help
PLANNER = COMMON + STAGES["Planner"].instructions + STAGES["Planner"].output_contract
FORMATTER = STAGES["Formatter"].instructions + SHARED.presentation_contract
ASSISTANT = COMMON + STAGES["AssistantOnWeb"].instructions


def expert_prompt(name: ExpertName, skills: list[str], *, allow_max=True) -> str:
    expert = EXPERTS[name]
    return (
        COMMON
        + BASE_SKILLS
        + SHARED.response_style
        + SHARED.output_contract
        + SHARED.presentation_contract
        + SHARED.visual_contract
        + f"\n角色：{expert.name}（{expert.translation}）。{expert.instructions}\n"
        + "\n".join(EXSKILL_HELP[s] for s in sorted(skills) if s in EXSKILL_HELP)
        + ("\n" + SHARED.max_thinking if allow_max and expert.allow_max_thinking else "")
    )

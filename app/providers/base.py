from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.agent.models import ExtractionResult, PolishingDraft


class LLMProvider(Protocol):
    name: str
    model: str

    async def extract_facts(
        self,
        message: str,
        context: dict[str, object],
    ) -> ExtractionResult:
        ...

    async def polish_text(self, draft: PolishingDraft) -> str:
        ...


def scenario_definition(
    context: dict[str, object],
    scenario_id: str,
) -> Mapping[str, object]:
    definitions = context.get("scenario_definitions")
    if isinstance(definitions, Mapping):
        selected = definitions.get(scenario_id)
        return selected if isinstance(selected, Mapping) else {}
    return context

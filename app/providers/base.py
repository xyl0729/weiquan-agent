from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.agent.models import (
    CaseContinuationContext,
    CaseContinuationResult,
    ExtractionResult,
    PolishingDraft,
)
from app.agent.grounding import (
    GroundedAnswerComposition,
    GroundingPacket,
)
from app.attachments.models import AttachmentEvidenceContext


class LLMProvider(Protocol):
    name: str
    model: str

    async def extract_facts(
        self,
        message: str,
        context: dict[str, object],
        evidence: tuple[AttachmentEvidenceContext, ...] = (),
        *,
        timeout_seconds: float | None = None,
    ) -> ExtractionResult:
        ...

    async def continue_case(
        self,
        message: str,
        context: CaseContinuationContext,
        evidence: tuple[AttachmentEvidenceContext, ...] = (),
        *,
        timeout_seconds: float | None = None,
    ) -> CaseContinuationResult:
        ...

    async def compose_grounded_answer(
        self,
        packet: GroundingPacket,
        *,
        timeout_seconds: float | None = None,
    ) -> GroundedAnswerComposition:
        ...

    async def polish_text(
        self,
        draft: PolishingDraft,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
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

from __future__ import annotations

from pathlib import Path
from typing import Final

from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from app.agent.errors import ProviderError
from app.agent.models import PolishingDraft
from app.providers.base import LLMProvider
from app.rendering.models import LockedConsultationDraft, RenderedDocuments


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ALLOWED_TEMPLATES: Final = frozenset(
    {
        "consultation_plan.j2",
        "evidence_request.j2",
    }
)


class PlanRenderer:
    def __init__(self, template_directory: Path) -> None:
        resolved = template_directory.resolve()
        if not resolved.is_relative_to(PROJECT_ROOT):
            raise ValueError("模板目录必须位于项目内")
        if not resolved.is_dir():
            raise FileNotFoundError(f"模板目录不存在: {resolved}")
        self.template_directory = resolved
        self._environment = SandboxedEnvironment(
            loader=FileSystemLoader(str(resolved), followlinks=False),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(
        self,
        draft: LockedConsultationDraft,
        *,
        plan_template: str = "consultation_plan.j2",
        evidence_template: str = "evidence_request.j2",
        polish_applied: bool = False,
    ) -> RenderedDocuments:
        plan_name = _allowed_template(plan_template)
        evidence_name = _allowed_template(evidence_template)
        context = {"plan": draft.plan}
        return RenderedDocuments(
            plan_text=self._environment.get_template(plan_name).render(context),
            evidence_request_text=(
                self._environment.get_template(evidence_name).render(context)
            ),
            polish_applied=polish_applied,
        )

    async def render_with_optional_polish(
        self,
        draft: LockedConsultationDraft,
        *,
        provider: LLMProvider | None,
        plan_template: str = "consultation_plan.j2",
        evidence_template: str = "evidence_request.j2",
    ) -> RenderedDocuments:
        if provider is None:
            return self.render(
                draft,
                plan_template=plan_template,
                evidence_template=evidence_template,
            )

        try:
            polished = await provider.polish_text(
                PolishingDraft(
                    text=draft.plan.communication_text,
                    purpose="communication",
                    tone="polite",
                )
            )
            normalized = polished.strip()
            if not normalized or len(normalized) > 2000:
                raise ValueError("润色结果长度无效")
        except (ProviderError, ValueError, TypeError):
            return self.render(
                draft,
                plan_template=plan_template,
                evidence_template=evidence_template,
            )

        polished_guide = draft.plan.communication_guide.model_copy(
            update={"message": normalized}
        )
        polished_plan = draft.plan.model_copy(
            update={
                "communication_text": normalized,
                "communication_guide": polished_guide,
            }
        )
        polished_draft = draft.model_copy(update={"plan": polished_plan})
        return self.render(
            polished_draft,
            plan_template=plan_template,
            evidence_template=evidence_template,
            polish_applied=True,
        )


def _allowed_template(name: str) -> str:
    if name not in ALLOWED_TEMPLATES:
        raise ValueError("模板名称不在固定白名单中")
    return name

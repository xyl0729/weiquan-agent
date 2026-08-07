from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.playbooks.loader import iter_playbook_documents
from app.playbooks.schema import (
    Playbook,
    SlotValidationResult,
    validate_facts,
)
from app.retrieval.database import connect_database, get_statute_by_ref


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class PlaybookRegistry:
    def __init__(self, playbooks: Iterable[Playbook]) -> None:
        items = list(playbooks)
        if not items:
            raise ValueError("至少需要一个正式 Playbook")

        self._by_id: dict[str, Playbook] = {}
        self._aliases: dict[str, str] = {}
        for playbook in items:
            if playbook.id in self._by_id:
                raise ValueError(f"Playbook ID 重复: {playbook.id}")
            self._by_id[playbook.id] = playbook
            for alias in [playbook.id, playbook.name, *playbook.aliases]:
                normalized = _normalize_alias(alias)
                existing = self._aliases.get(normalized)
                if existing is not None and existing != playbook.id:
                    raise ValueError(
                        f"Playbook 别名冲突: {alias} "
                        f"({existing}, {playbook.id})"
                    )
                self._aliases[normalized] = playbook.id

    @classmethod
    def from_directory(cls, directory: Path) -> "PlaybookRegistry":
        resolved = directory.resolve()
        if not resolved.is_relative_to(PROJECT_ROOT):
            raise ValueError("Playbook 目录必须位于项目内")

        playbooks: list[Playbook] = []
        for path, document in iter_playbook_documents(resolved):
            if document.get("development_only") is True:
                continue
            try:
                playbooks.append(Playbook.model_validate(document))
            except ValidationError as exc:
                raise ValueError(f"Playbook 校验失败 {path.name}: {exc}") from exc
        return cls(playbooks)

    @property
    def scenario_ids(self) -> list[str]:
        return sorted(self._by_id)

    @property
    def playbooks(self) -> list[Playbook]:
        return [self._by_id[key] for key in sorted(self._by_id)]

    def get(self, scenario_id: str) -> Playbook:
        try:
            return self._by_id[scenario_id]
        except KeyError as exc:
            raise LookupError(f"未知场景: {scenario_id}") from exc

    def resolve(self, value: str) -> Playbook | None:
        scenario_id = self._aliases.get(_normalize_alias(value))
        return self._by_id.get(scenario_id) if scenario_id else None

    def validate_facts(
        self,
        scenario_id: str,
        facts: dict[str, Any],
        *,
        apply_defaults: bool = False,
    ) -> SlotValidationResult:
        return validate_facts(
            self.get(scenario_id),
            facts,
            apply_defaults=apply_defaults,
        )

    def provider_context(
        self,
        *,
        scenario_id: str | None = None,
        existing_facts: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        if scenario_id is not None:
            playbooks = [self.get(scenario_id)]
        else:
            playbooks = self.playbooks

        scenario_definitions: dict[str, dict[str, object]] = {}
        for playbook in playbooks:
            scenario_slots = {
                slot.name: slot.model_dump(exclude_none=True)
                for slot in playbook.slots.all
            }
            scenario_definitions[playbook.id] = {
                "name": playbook.name,
                "aliases": list(playbook.aliases),
                "allowed_slot_names": sorted(scenario_slots),
                "required_slot_names": list(
                    playbook.required_slot_names
                ),
                "slot_definitions": scenario_slots,
            }

        # Keep the flat fields for callers that still provide one scenario.
        slots = {
            slot.name: slot.model_dump(exclude_none=True)
            for playbook in playbooks
            for slot in playbook.slots.all
        }
        required = list(
            dict.fromkeys(
                slot.name
                for playbook in playbooks
                for slot in playbook.slots.required
            )
        )
        return {
            "allowed_scenario_ids": [playbook.id for playbook in playbooks],
            "current_scenario_id": scenario_id,
            "allowed_slot_names": sorted(slots),
            "required_slot_names": required,
            "slot_definitions": slots,
            "scenario_definitions": scenario_definitions,
            "existing_facts": existing_facts or {},
        }

    def verify_references(self, database_path: Path) -> None:
        connection = connect_database(database_path)
        missing: list[str] = []
        try:
            for playbook in self.playbooks:
                for basis in playbook.legal_basis:
                    if get_statute_by_ref(connection, basis.ref) is None:
                        missing.append(f"{playbook.id}: {basis.ref}")
        finally:
            connection.close()
        if missing:
            raise ValueError(
                "正式 Playbook 引用未命中: " + ", ".join(missing)
            )


def _normalize_alias(value: str) -> str:
    return "".join(value.strip().casefold().split())

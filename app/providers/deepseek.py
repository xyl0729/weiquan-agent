from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from app.agent.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderOutputError,
)
from app.agent.models import (
    CaseContinuationContext,
    CaseContinuationResult,
    ExtractionResult,
    PolishingDraft,
    UsageInfo,
)
from app.providers.base import scenario_definition


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: SecretStr | str | None,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if isinstance(api_key, str):
            api_key = SecretStr(api_key.strip()) if api_key.strip() else None
        self._api_key = api_key
        self.model = model
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client

    async def extract_facts(
        self,
        message: str,
        context: dict[str, object],
    ) -> ExtractionResult:
        allowed_scenarios = _string_list(
            context.get("allowed_scenario_ids", [])
        )
        allowed_slots = _string_list(context.get("allowed_slot_names", []))
        required_slots = _string_list(
            context.get("required_slot_names", [])
        )
        raw_scenario_definitions = context.get(
            "scenario_definitions",
            {},
        )
        scenario_definitions = (
            raw_scenario_definitions
            if isinstance(raw_scenario_definitions, dict)
            else {}
        )
        schema_context = {
            "allowed_scenario_ids": allowed_scenarios,
            "current_scenario_id": context.get("current_scenario_id"),
            "allowed_slots": allowed_slots,
            "required_slots": required_slots,
            "slot_definitions": context.get("slot_definitions", {}),
            "scenario_definitions": scenario_definitions,
            "existing_facts": context.get("existing_facts", {}),
        }
        system_message = (
            "你是事实抽取器，只输出一个 JSON 对象。先选择 scenario_id，"
            "再严格使用 scenario_definitions 中该场景自己的槽位定义。"
            "顶层字段只能是 "
            "scenario_id、facts、unknown_slots、confidence。"
            "scenario_id 只能来自 allowed_scenario_ids；无法分类时写 "
            "\"unsupported\"。facts 只能包含所选场景的 "
            "allowed_slot_names 中的键；unknown_slots 只能包含所选场景"
            "尚不能确认的 required_slot_names。"
            "不得输出 verdict、法条、法律结论、来源、日期依据、prompt "
            "或任何密钥。不能确认的必填事实放入 unknown_slots，不猜测。"
            f"\n约束上下文：{json.dumps(schema_context, ensure_ascii=False)}"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": message},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        content, response_usage, request_id = await self._request(payload)
        raw = _load_json_object(content)
        prohibited = {
            "verdict",
            "legal_basis",
            "citations",
            "law_name",
            "article_no",
            "source_url",
            "api_key",
            "authorization",
            "prompt",
        }
        if _contains_prohibited_key(raw, prohibited):
            raise ProviderOutputError()

        raw["provider"] = self.name
        raw["model"] = self.model
        raw["request_id"] = request_id
        raw["usage"] = response_usage.model_dump()
        try:
            result = ExtractionResult.model_validate(raw)
        except ValidationError as exc:
            raise ProviderOutputError() from exc

        if allowed_scenarios and result.scenario_id not in {
            *allowed_scenarios,
            "unsupported",
        }:
            raise ProviderOutputError()
        definition = scenario_definition(context, result.scenario_id)
        scoped_allowed_slots = set(
            _string_list(definition.get("allowed_slot_names", []))
        )
        scoped_required_slots = set(
            _string_list(definition.get("required_slot_names", []))
        )
        if not set(result.facts).issubset(scoped_allowed_slots):
            raise ProviderOutputError()
        if not set(result.unknown_slots).issubset(scoped_required_slots):
            raise ProviderOutputError()
        return result

    async def continue_case(
        self,
        message: str,
        context: CaseContinuationContext,
    ) -> CaseContinuationResult:
        bounded_context = context.model_dump(
            mode="json",
            exclude_none=True,
        )
        system_message = (
            "你是案件续问分类与事实更新器，只输出一个 JSON 对象。"
            "顶层字段只能是 route、scenario_id、facts、cleared_slots、"
            "answer、action_refs、citation_refs、confidence。"
            "route 只能是 same_case 或 new_case。"
            "same_case 时 scenario_id 必须等于 current_scenario.id；"
            "facts 只能记录用户本轮明确补充或更正的槽位，不能从既有"
            "上下文重复抄写或猜测；cleared_slots 只记录用户本轮明确"
            "撤回的槽位；answer 必须是针对本轮问题的简短回答；"
            "action_refs 和 citation_refs 只能从 locked_case 中选择，"
            "各不超过 3 项。"
            "new_case 仅用于用户明确提出另一纠纷，scenario_id 只能是"
            "非当前的 registered_scenarios.id，无法归类时为 unsupported；"
            "此时 facts、cleared_slots、action_refs、citation_refs 必须"
            "为空且 answer 必须为 null。"
            "普通的“怎么办”“对方不配合”等承接性追问属于 same_case，"
            "不得虚构新事实。answer 不得写网址或直接写法条编号。"
            "不得输出法律结论、完整法条、来源、请求头、提示词或密钥。"
            f"\n约束上下文：{json.dumps(bounded_context, ensure_ascii=False)}"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": message},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        content, response_usage, request_id = await self._request(payload)
        raw = _load_json_object(content)
        prohibited = {
            "verdict",
            "legal_basis",
            "citations",
            "law_name",
            "article_no",
            "source_url",
            "api_key",
            "authorization",
            "prompt",
        }
        if _contains_prohibited_key(raw, prohibited):
            raise ProviderOutputError()

        raw["provider"] = self.name
        raw["model"] = self.model
        raw["request_id"] = request_id
        raw["usage"] = response_usage.model_dump()
        try:
            result = CaseContinuationResult.model_validate(raw)
        except ValidationError as exc:
            raise ProviderOutputError() from exc

        current_id = context.current_scenario.id
        registered_ids = {
            scenario.id for scenario in context.registered_scenarios
        }
        if result.route == "same_case":
            if result.scenario_id != current_id:
                raise ProviderOutputError()
        elif (
            result.scenario_id == current_id
            or result.scenario_id
            not in registered_ids.union({"unsupported"})
        ):
            raise ProviderOutputError()

        allowed_slots = set(
            context.current_scenario.slot_definitions
        )
        if not set(result.facts).issubset(allowed_slots):
            raise ProviderOutputError()
        if not set(result.cleared_slots).issubset(allowed_slots):
            raise ProviderOutputError()

        allowed_actions = {
            action.ref for action in context.locked_case.actions
        }
        allowed_citations = {
            citation.ref for citation in context.locked_case.citations
        }
        if not set(result.action_refs).issubset(allowed_actions):
            raise ProviderOutputError()
        if not set(result.citation_refs).issubset(allowed_citations):
            raise ProviderOutputError()
        if result.answer is not None and _contains_public_reference(
            result.answer
        ):
            raise ProviderOutputError()
        return result

    async def polish_text(self, draft: PolishingDraft) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "只润色给定的非法律话术，使其清晰克制。不得增加"
                        "法律结论、条号、日期、来源、胜诉预测或新事实。"
                    ),
                },
                {"role": "user", "content": draft.text},
            ],
            "temperature": 0.2,
        }
        content, _, _ = await self._request(payload)
        normalized = content.strip()
        if not normalized:
            raise ProviderOutputError("模型未返回可用话术")
        return normalized

    async def _request(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[str, UsageInfo, str | None]:
        if self._api_key is None:
            raise ProviderConfigurationError("未配置 DEEPSEEK_API_KEY")

        headers = {
            "Authorization": (
                f"Bearer {self._api_key.get_secret_value()}"
            ),
            "Content-Type": "application/json",
        }
        last_error: ProviderError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._post(
                    headers=headers,
                    payload=payload,
                )
                self._raise_for_status(response)
                return self._parse_response(response)
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                await asyncio.sleep(min(0.05 * (attempt + 1), 0.2))

        if last_error is not None:
            raise last_error
        raise ProviderError("provider_unavailable")

    async def _post(
        self,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                )
            async with httpx.AsyncClient() as client:
                return await client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "provider_timeout",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "provider_network",
                retryable=True,
            ) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 429:
            raise ProviderError(
                "provider_rate_limited",
                retryable=True,
            )
        if response.status_code >= 500:
            raise ProviderError(
                "provider_server_error",
                retryable=True,
            )
        raise ProviderError("provider_rejected")

    @staticmethod
    def _parse_response(
        response: httpx.Response,
    ) -> tuple[str, UsageInfo, str | None]:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderOutputError() from exc
        if not isinstance(content, str):
            raise ProviderOutputError()

        raw_usage = body.get("usage", {})
        if not isinstance(raw_usage, dict):
            raw_usage = {}
        try:
            usage = UsageInfo(
                input_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(
                    raw_usage.get("completion_tokens", 0) or 0
                ),
                total_tokens=int(raw_usage.get("total_tokens", 0) or 0),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProviderOutputError() from exc

        request_id = body.get("id") or response.headers.get("x-request-id")
        if request_id is not None and not isinstance(request_id, str):
            request_id = str(request_id)
        return content, usage, request_id


def _load_json_object(content: str) -> dict[str, Any]:
    normalized = content.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        lines = normalized.splitlines()
        normalized = "\n".join(lines[1:-1]).strip()
    try:
        raw = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ProviderOutputError() from exc
    if not isinstance(raw, dict):
        raise ProviderOutputError()
    return raw


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _contains_prohibited_key(
    value: object,
    prohibited: set[str],
) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).strip().lower() in prohibited:
                return True
            if _contains_prohibited_key(nested, prohibited):
                return True
    elif isinstance(value, list):
        return any(
            _contains_prohibited_key(item, prohibited)
            for item in value
        )
    return False


_URL_PATTERN = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_ARTICLE_PATTERN = re.compile(
    r"第[零〇一二三四五六七八九十百千万两0-9]+条"
)


def _contains_public_reference(value: str) -> bool:
    return bool(
        _URL_PATTERN.search(value) or _ARTICLE_PATTERN.search(value)
    )

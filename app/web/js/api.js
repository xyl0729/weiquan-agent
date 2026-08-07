const PIPELINE_STATUSES = new Set([
  "need_more_facts",
  "ready",
  "escalate",
]);

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export class ApiError extends Error {
  constructor({
    code,
    message,
    userMessage,
    status = 0,
    retryable = false,
  }) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.userMessage = userMessage;
  }
}

export async function getHealth() {
  return requestJson("/health", {}, validateHealth);
}

export async function listSessions() {
  return requestJson("/api/sessions", {}, validateSessionList);
}

export async function getSession(sessionId) {
  assertUuid(sessionId);
  return requestJson(
    `/api/sessions/${encodeURIComponent(sessionId)}`,
    {},
    validateSessionDetail,
  );
}

export async function deleteSession(sessionId) {
  assertUuid(sessionId);
  await requestNoContent(
    `/api/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
}

export async function consult({ message, sessionId = null }) {
  const body = { message };
  if (sessionId !== null) {
    assertUuid(sessionId);
    body.session_id = sessionId;
  }
  return requestJson(
    "/api/consult",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
    validateConsultResponse,
  );
}

async function requestJson(path, options, validate) {
  const response = await safeFetch(path, options);
  if (!response.ok) {
    throw await responseError(response);
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    throw invalidResponseError();
  }

  try {
    return validate(payload);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    throw invalidResponseError();
  }
}

async function requestNoContent(path, options) {
  const response = await safeFetch(path, options);
  if (!response.ok) {
    throw await responseError(response);
  }
  if (response.status !== 204) {
    throw invalidResponseError();
  }
}

async function safeFetch(path, options) {
  const headers = {
    Accept: "application/json",
    ...(options.body ? { "Content-Type": "application/json" } : {}),
  };

  try {
    return await fetch(path, {
      ...options,
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError({
      code: "offline",
      message: "Network request failed",
      userMessage: "无法连接本地服务，请确认服务正在运行后重试。",
      retryable: true,
    });
  }
}

async function responseError(response) {
  let code = "request_failed";
  try {
    const payload = await response.json();
    if (
      isObject(payload) &&
      isObject(payload.detail) &&
      typeof payload.detail.code === "string"
    ) {
      code = payload.detail.code;
    }
  } catch {
    // The UI never displays an unstructured response body.
  }

  if (code === "session_not_found") {
    return new ApiError({
      code,
      status: response.status,
      message: "Session not found",
      userMessage: "这条咨询不存在或已经过期。",
    });
  }
  if (response.status === 429) {
    return new ApiError({
      code,
      status: response.status,
      message: "Rate limit reached",
      userMessage: "当前咨询额度已达到限制，请稍后再试。",
      retryable: true,
    });
  }
  if (response.status === 503) {
    return new ApiError({
      code,
      status: response.status,
      message: "Service unavailable",
      userMessage: "咨询服务暂时不可用，请稍后重试。",
      retryable: true,
    });
  }
  if (response.status >= 500) {
    return new ApiError({
      code,
      status: response.status,
      message: "Server response failed",
      userMessage: "服务处理出现问题，本次未完成的结果没有展示。",
      retryable: true,
    });
  }
  if (response.status === 422) {
    return new ApiError({
      code,
      status: response.status,
      message: "Request validation failed",
      userMessage: "提交的内容无法处理，请检查后重试。",
    });
  }
  return new ApiError({
    code,
    status: response.status,
    message: "Request failed",
    userMessage: "请求没有完成，请稍后重试。",
    retryable: response.status >= 500,
  });
}

function validateHealth(value) {
  const payload = expectObject(value);
  expectString(payload.status);
  const checks = expectObject(payload.checks);
  for (const result of Object.values(checks)) {
    expectString(result);
  }
  return payload;
}

function validateSessionList(value) {
  const payload = expectObject(value);
  if (!Array.isArray(payload.sessions)) {
    throw invalidResponseError();
  }
  payload.sessions.forEach(validateSessionSummary);
  return payload;
}

function validateSessionDetail(value) {
  const payload = expectObject(value);
  validateSessionSummary(payload.session);
  if (!Array.isArray(payload.turns) || payload.turns.length === 0) {
    throw invalidResponseError();
  }
  for (const turn of payload.turns) {
    const item = expectObject(turn);
    assertUuid(item.turn_id);
    expectString(item.user_message);
    expectString(item.created_at);
    validateConsultResponse(item.response);
    if (
      item.response.session_id !== payload.session.session_id ||
      item.response.turn_id !== item.turn_id
    ) {
      throw invalidResponseError();
    }
  }
  return payload;
}

function validateSessionSummary(value) {
  const item = expectObject(value);
  assertUuid(item.session_id);
  expectString(item.title);
  if (
    item.scenario_id !== null &&
    typeof item.scenario_id !== "string"
  ) {
    throw invalidResponseError();
  }
  validatePipelineStatus(item.status);
  expectString(item.created_at);
  expectString(item.updated_at);
  expectString(item.expires_at);
  return item;
}

function validateConsultResponse(value) {
  const payload = expectObject(value);
  assertUuid(payload.session_id);
  assertUuid(payload.turn_id);
  assertUuid(payload.audit_id);
  if (
    !Number.isInteger(payload.followup_round) ||
    payload.followup_round < 0 ||
    payload.followup_round > 2 ||
    typeof payload.can_ask_more !== "boolean"
  ) {
    throw invalidResponseError();
  }
  validatePipelineStatus(payload.status);
  validateStringArray(payload.questions);
  validateStringArray(payload.limitations);

  if (payload.verdict !== null) {
    const verdict = expectObject(payload.verdict);
    expectString(verdict.code);
    expectString(verdict.label);
    validatePipelineStatus(verdict.status);
    validateStringArray(verdict.rule_ids);
    expectString(verdict.key_point);
  }

  if (payload.plan !== null) {
    validatePlan(payload.plan);
  }

  if (!Array.isArray(payload.citations)) {
    throw invalidResponseError();
  }
  payload.citations.forEach(validateCitation);

  const usage = expectObject(payload.usage);
  expectString(usage.provider);
  expectString(usage.model);
  for (const field of [
    "input_tokens",
    "output_tokens",
    "total_tokens",
  ]) {
    if (!Number.isInteger(usage[field]) || usage[field] < 0) {
      throw invalidResponseError();
    }
  }
  return payload;
}

function validatePlan(value) {
  const plan = expectObject(value);
  expectString(plan.summary);
  validateStringArray(plan.evidence_now);
  validateStringArray(plan.actions);
  expectString(plan.communication_text);
  validateStringArray(plan.limitations);

  if (plan.time_limit !== null) {
    const timeLimit = expectObject(plan.time_limit);
    expectString(timeLimit.label);
    expectString(timeLimit.status);
    expectString(timeLimit.legal_ref);
    expectString(timeLimit.reminder);
  }

  const jurisdiction = expectObject(plan.jurisdiction);
  expectString(jurisdiction.status);
  validateStringArray(jurisdiction.notices);
}

function validateCitation(value) {
  const citation = expectObject(value);
  for (const field of [
    "ref",
    "law_name",
    "article_no",
    "content",
    "effective_date",
    "source_url",
  ]) {
    expectString(citation[field]);
  }
  let source;
  try {
    source = new URL(citation.source_url);
  } catch {
    throw invalidResponseError();
  }
  if (!["http:", "https:"].includes(source.protocol)) {
    throw invalidResponseError();
  }
}

function validatePipelineStatus(value) {
  if (!PIPELINE_STATUSES.has(value)) {
    throw invalidResponseError();
  }
}

function validateStringArray(value) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw invalidResponseError();
  }
}

function expectObject(value) {
  if (!isObject(value)) {
    throw invalidResponseError();
  }
  return value;
}

function expectString(value) {
  if (typeof value !== "string" || value.length === 0) {
    throw invalidResponseError();
  }
  return value;
}

function assertUuid(value) {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
    throw invalidResponseError();
  }
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function invalidResponseError() {
  return new ApiError({
    code: "invalid_response",
    status: 500,
    message: "Response shape is invalid",
    userMessage: "服务返回的数据未通过完整性检查，已停止展示。",
    retryable: true,
  });
}

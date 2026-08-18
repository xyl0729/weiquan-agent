const PIPELINE_STATUSES = new Set([
  "need_more_facts",
  "ready",
  "escalate",
]);
const TURN_KINDS = new Set([
  "fact_collection",
  "initial_plan",
  "plan_update",
  "followup_answer",
  "new_case",
  "unverified_guidance",
  "emergency_guidance",
]);
const COVERAGE_MODES = new Set([
  "formal",
  "unverified_guidance",
  "emergency_guidance",
]);
const RISK_FLAGS = new Set([
  "immediate_danger",
  "minor_harm",
  "urgent_medical",
  "suspected_crime",
  "fraud_loss",
  "evidence_loss",
]);
const ATTACHMENT_STATUSES = new Set([
  "processing",
  "review_required",
  "confirmed",
  "failed",
  "bound",
]);
const ATTACHMENT_MEDIA_TYPES = new Set([
  "application/pdf",
  "image/png",
  "image/jpeg",
]);
const EXTRACTION_METHODS = new Set([
  "direct_text",
  "ocr",
  "mixed",
]);
const ATTACHMENT_ERROR_CODES = new Set([
  "attachment_type_unsupported",
  "attachment_type_mismatch",
  "attachment_name_invalid",
  "attachment_pdf_encrypted",
  "attachment_corrupt",
  "attachment_text_empty",
  "attachment_too_large",
  "attachment_page_limit_exceeded",
  "attachment_pixel_limit_exceeded",
  "attachment_extracted_text_too_long",
  "attachment_extraction_timeout",
  "attachment_not_found",
  "attachment_not_reviewable",
  "attachment_not_confirmed",
  "attachment_already_bound",
  "attachment_count_exceeded",
  "attachment_context_too_long",
  "attachment_service_unavailable",
]);
const REVIEW_ATTACHMENT_FIELDS = new Set([
  "id",
  "status",
  "original_name",
  "media_type",
  "size_bytes",
  "page_count",
  "extraction_method",
  "blocks",
  "warnings",
  "confirmed_text",
  "error_code",
]);
const TURN_ATTACHMENT_FIELDS = new Set([
  "id",
  "status",
  "original_name",
  "media_type",
  "size_bytes",
  "page_count",
  "extraction_method",
  "warnings",
  "confirmed_text",
]);
const EXTRACTION_BLOCK_FIELDS = new Set([
  "page_number",
  "block_index",
  "text",
  "confidence",
]);

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const WARNING_CODE_PATTERN = /^[a-z][a-z0-9_]{1,63}$/;
const USER_STATUSES = new Set([
  "pending_verification",
  "active",
  "disabled",
]);
const USER_ROLES = new Set(["user", "admin"]);
const RUNTIME_IDENTITY_MODES = new Set([
  "local_full_test",
  "account",
]);
const RUNTIME_CONFIG_FIELDS = new Set(["identity_mode"]);
const CONSULT_TIMEOUT_MS = 35_000;
let csrfToken = "";

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

export async function getRuntimeConfig() {
  return requestJson(
    "/api/runtime-config",
    {},
    validateRuntimeConfig,
  );
}

export async function getCaptchaConfig() {
  return requestJson(
    "/api/auth/captcha-config",
    {},
    validateCaptchaConfig,
  );
}

export async function registerAccount({
  email,
  password,
  captchaToken,
  privacyVersion,
}) {
  const body = {
    email,
    password,
    privacy_version: privacyVersion,
    privacy_accepted: true,
  };
  if (captchaToken !== null) {
    body.captcha_token = captchaToken;
  }
  return requestJson(
    "/api/auth/register",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
    validateAccepted,
  );
}

export async function resendVerification(email) {
  return requestJson(
    "/api/auth/resend-verification",
    {
      method: "POST",
      body: JSON.stringify({ email }),
    },
    validateAccepted,
  );
}

export async function verifyEmail({ email, code }) {
  return requestJson(
    "/api/auth/verify",
    {
      method: "POST",
      body: JSON.stringify({ email, code }),
    },
    validateAuthProjection,
  );
}

export async function loginAccount({ email, password }) {
  const projection = await requestJson(
    "/api/auth/login",
    {
      method: "POST",
      body: JSON.stringify({ email, password }),
    },
    validateAuthProjection,
  );
  await refreshCsrfToken();
  return projection;
}

export async function getCurrentAccount() {
  return requestJson(
    "/api/auth/me",
    {},
    validateAuthProjection,
  );
}

export async function refreshCsrfToken() {
  const payload = await requestJson(
    "/api/auth/csrf",
    {},
    validateCsrf,
  );
  csrfToken = payload.csrf_token;
  return csrfToken;
}

export function clearCsrfToken() {
  csrfToken = "";
}

export async function logoutAccount() {
  await requestNoContent(
    "/api/auth/logout",
    { method: "POST" },
  );
  clearCsrfToken();
}

export async function requestPasswordReset(email) {
  return requestJson(
    "/api/auth/forgot-password",
    {
      method: "POST",
      body: JSON.stringify({ email }),
    },
    validateAccepted,
  );
}

export async function resetPassword({ token, newPassword }) {
  await requestNoContent(
    "/api/auth/reset-password",
    {
      method: "POST",
      body: JSON.stringify({
        token,
        new_password: newPassword,
      }),
    },
  );
  clearCsrfToken();
}

export async function startTrial({
  captchaToken = null,
  privacyVersion = "",
  privacyAccepted = false,
} = {}) {
  const body = {};
  if (captchaToken !== null) {
    body.captcha_token = captchaToken;
  }
  if (privacyVersion || privacyAccepted) {
    body.privacy_version = privacyVersion;
    body.privacy_accepted = privacyAccepted;
  }
  return requestJson(
    "/api/trial/start",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
    validateTrialStart,
  );
}

export async function trialConsult({
  message,
  sessionId = null,
  jurisdiction = null,
}) {
  const body = { message };
  if (sessionId !== null) {
    assertUuid(sessionId);
    body.session_id = sessionId;
  }
  if (jurisdiction !== null) {
    body.jurisdiction = jurisdiction;
  }
  return requestConsultJson(
    "/api/trial/consult",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export async function getPrivacyPolicy() {
  return requestJson(
    "/api/privacy",
    {},
    validatePrivacyPolicy,
  );
}

export async function acceptPrivacy(policyVersion) {
  return requestJson(
    "/api/privacy/accept",
    {
      method: "POST",
      body: JSON.stringify({
        context: "consultation",
        policy_version: policyVersion,
      }),
    },
    validatePrivacyPolicy,
  );
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

export async function uploadAttachment(file) {
  if (
    file === null ||
    typeof file !== "object" ||
    typeof file.name !== "string"
  ) {
    throw invalidRequestError("请选择需要上传的文件。");
  }
  const body = new FormData();
  body.append("file", file, file.name);
  return requestJson(
    "/api/attachments",
    {
      method: "POST",
      body,
    },
    validateAttachmentReview,
  );
}

export async function getAttachment(attachmentId) {
  assertUuid(attachmentId);
  return requestJson(
    `/api/attachments/${encodeURIComponent(attachmentId)}`,
    {},
    (value) => validateRequestedAttachment(value, attachmentId),
  );
}

export async function confirmAttachment(
  attachmentId,
  confirmedText,
) {
  assertUuid(attachmentId);
  if (
    typeof confirmedText !== "string" ||
    confirmedText.trim().length === 0
  ) {
    throw invalidRequestError("确认文字不能为空。");
  }
  return requestJson(
    `/api/attachments/${encodeURIComponent(attachmentId)}`,
    {
      method: "PATCH",
      body: JSON.stringify({
        confirmed_text: confirmedText,
      }),
    },
    (value) =>
      validateRequestedAttachment(value, attachmentId, "confirmed"),
  );
}

export async function deleteAttachment(attachmentId) {
  assertUuid(attachmentId);
  await requestNoContent(
    `/api/attachments/${encodeURIComponent(attachmentId)}`,
    { method: "DELETE" },
  );
}

export async function consult({
  message,
  sessionId = null,
  attachmentIds = [],
}) {
  validateAttachmentIds(attachmentIds);
  const body = {
    message,
    attachment_ids: attachmentIds,
  };
  if (sessionId !== null) {
    assertUuid(sessionId);
    body.session_id = sessionId;
  }
  return requestConsultJson(
    "/api/consult",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

async function requestConsultJson(path, options) {
  const controller = new AbortController();
  const timeoutId = setTimeout(
    () => controller.abort(),
    CONSULT_TIMEOUT_MS,
  );
  try {
    return await requestJson(
      path,
      {
        ...options,
        signal: controller.signal,
      },
      validateConsultResponse,
    );
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError({
        code: "consult_timeout",
        message: "Consultation request timed out",
        userMessage: "本次咨询等待时间较长，请稍后重试。",
        retryable: true,
      });
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
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
  const headers = new Headers(options.headers || {});
  const method = String(options.method || "GET").toUpperCase();
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  if (
    options.body &&
    !(options.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  if (
    csrfToken &&
    !["GET", "HEAD", "OPTIONS"].includes(method) &&
    !headers.has("X-CSRF-Token")
  ) {
    headers.set("X-CSRF-Token", csrfToken);
  }

  try {
    return await fetch(path, {
      ...options,
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (error) {
    if (
      error?.name === "AbortError" ||
      options.signal?.aborted
    ) {
      throw error;
    }
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
  if (code === "provider_unavailable") {
    return new ApiError({
      code,
      status: response.status,
      message: "Consultation service is unavailable",
      userMessage: "咨询服务当前繁忙，请稍后重试。",
      retryable: true,
    });
  }
  const safeMessages = {
    invalid_credentials: "邮箱或密码错误。",
    registration_required: "请先注册或登录后使用此功能。",
    csrf_invalid: "页面安全校验已过期，请刷新后重试。",
    same_origin_required: "请求来源校验失败，请刷新页面后重试。",
    registration_capacity_full: "当前公测名额已满。",
    public_registration_closed: "公开注册尚未开放，请稍后再试。",
    captcha_failed: "人机验证未通过，请重新验证。",
    mail_unavailable: "邮件暂时无法发送，请稍后重试。",
    token_invalid: "链接无效或已过期，请重新申请。",
    password_invalid: "密码长度必须为 8 至 128 个字符。",
    email_invalid: "请输入有效的邮箱地址。",
    privacy_acceptance_required: "请先确认当前隐私政策。",
    auth_rate_limited: "操作过于频繁，请稍后重试。",
    trial_identity_required: "请先确认隐私政策并领取试用次数。",
    trial_identity_limit_exceeded: "当前设备暂时无法领取新的试用次数。",
    trial_quota_exceeded: "5 次试用已经用完，注册后可继续使用。",
    trial_daily_capacity_exceeded: "今日匿名试用名额已用完，请明天再试。",
    registered_daily_quota_exceeded: "今日 10 次咨询额度已经用完。",
    registered_monthly_quota_exceeded: "本月 50 次咨询额度已经用完。",
    new_work_paused:
      "当前暂停新的咨询和附件上传，请稍后再试。",
    service_capacity_full: "当前咨询排队已满，请稍后再试。",
    case_no_progress:
      "当前信息下没有新的处理步骤。请补充对方回复、新材料、新事件或风险变化后再继续。",
    consultation_conflict:
      "这条咨询刚刚有了更新，请重新提交本次追问。",
  };
  if (Object.hasOwn(safeMessages, code)) {
    return new ApiError({
      code,
      status: response.status,
      message: "Application request was rejected",
      userMessage: safeMessages[code],
      retryable: [
        "mail_unavailable",
        "auth_rate_limited",
        "trial_daily_capacity_exceeded",
        "new_work_paused",
        "service_capacity_full",
        "consultation_conflict",
      ].includes(code),
    });
  }
  const attachmentMessage = attachmentErrorMessage(code);
  if (attachmentMessage !== null) {
    return new ApiError({
      code,
      status: response.status,
      message: "Attachment request failed",
      userMessage: attachmentMessage,
      retryable: [
        "attachment_extraction_timeout",
        "attachment_service_unavailable",
      ].includes(code),
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

function validateRuntimeConfig(value) {
  const payload = expectObject(value);
  expectExactFields(payload, RUNTIME_CONFIG_FIELDS);
  if (!RUNTIME_IDENTITY_MODES.has(payload.identity_mode)) {
    throw invalidResponseError();
  }
  return payload;
}

function validateCaptchaConfig(value) {
  const payload = expectObject(value);
  if (
    typeof payload.enabled !== "boolean" ||
    typeof payload.scene_id !== "string" ||
    typeof payload.prefix !== "string" ||
    payload.region !== "cn" ||
    (
      payload.enabled &&
      (!payload.scene_id.trim() || !payload.prefix.trim())
    ) ||
    (
      !payload.enabled &&
      (payload.scene_id !== "" || payload.prefix !== "")
    )
  ) {
    throw invalidResponseError();
  }
  return payload;
}

function validateAccepted(value) {
  const payload = expectObject(value);
  if (payload.status !== "accepted") {
    throw invalidResponseError();
  }
  return payload;
}

function validateAuthProjection(value) {
  const payload = expectObject(value);
  const user = expectObject(payload.user);
  for (const field of ["id", "email", "created_at"]) {
    expectString(user[field]);
  }
  if (
    !USER_STATUSES.has(user.status) ||
    !USER_ROLES.has(user.role) ||
    (
      user.verified_at !== null &&
      typeof user.verified_at !== "string"
    ) ||
    typeof payload.privacy_acceptance_required !== "boolean"
  ) {
    throw invalidResponseError();
  }
  expectString(payload.privacy_version);
  validateRegisteredQuota(payload.quota);
  return payload;
}

function validateCsrf(value) {
  const payload = expectObject(value);
  if (
    typeof payload.csrf_token !== "string" ||
    payload.csrf_token.length < 32
  ) {
    throw invalidResponseError();
  }
  return payload;
}

function validateTrialStart(value) {
  const payload = expectObject(value);
  assertUuid(payload.identity_id);
  validateTrialQuota(payload.quota);
  return payload;
}

function validatePrivacyPolicy(value) {
  const payload = expectObject(value);
  expectString(payload.version);
  expectString(payload.text);
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
  if (!TURN_KINDS.has(payload.turn_kind)) {
    throw invalidResponseError();
  }
  validateStringArray(payload.questions);
  validateStringArray(payload.limitations);

  if (payload.coverage !== null) {
    validateCoverage(payload.coverage);
  }

  if (payload.guidance !== null) {
    validateGuidance(payload.guidance);
  }

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

  if (payload.reply !== null) {
    validateReply(payload.reply);
  }

  if (!Array.isArray(payload.citations)) {
    throw invalidResponseError();
  }
  payload.citations.forEach(validateCitation);
  if (
    !Array.isArray(payload.attachments) ||
    payload.attachments.length > 3
  ) {
    throw invalidResponseError();
  }
  const attachmentIds = payload.attachments.map((attachment) => {
    const item = validateAttachmentTurn(attachment);
    return item.id;
  });
  if (new Set(attachmentIds).size !== attachmentIds.length) {
    throw invalidResponseError();
  }
  validateTurnCombination(payload);

  const usage = expectObject(payload.usage);
  expectString(usage.provider);
  expectString(usage.model);
  if (
    usage.request_id !== null &&
    (
      typeof usage.request_id !== "string" ||
      usage.request_id.length === 0
    )
  ) {
    throw invalidResponseError();
  }
  for (const field of [
    "input_tokens",
    "output_tokens",
    "total_tokens",
  ]) {
    if (!Number.isInteger(usage[field]) || usage[field] < 0) {
      throw invalidResponseError();
    }
  }
  if (
    usage.total_tokens <
      usage.input_tokens + usage.output_tokens ||
    (
      usage.estimated_cost_usd !== null &&
      (
        typeof usage.estimated_cost_usd !== "number" ||
        !Number.isFinite(usage.estimated_cost_usd) ||
        usage.estimated_cost_usd < 0
      )
    )
  ) {
    throw invalidResponseError();
  }
  if (payload.quota !== null && payload.quota !== undefined) {
    if (Object.hasOwn(payload.quota, "remaining_total")) {
      validateTrialQuota(payload.quota);
    } else {
      validateRegisteredQuota(payload.quota);
    }
  }
  return payload;
}

function validateTrialQuota(value) {
  const quota = expectObject(value);
  if (
    !Number.isInteger(quota.remaining_total) ||
    quota.remaining_total < 0 ||
    quota.remaining_total > 5
  ) {
    throw invalidResponseError();
  }
  return quota;
}

function validateRegisteredQuota(value) {
  const quota = expectObject(value);
  if (
    !Number.isInteger(quota.remaining_daily) ||
    quota.remaining_daily < 0 ||
    quota.remaining_daily > 10 ||
    !Number.isInteger(quota.remaining_monthly) ||
    quota.remaining_monthly < 0 ||
    quota.remaining_monthly > 50
  ) {
    throw invalidResponseError();
  }
  expectString(quota.day_resets_at);
  expectString(quota.month_resets_at);
  return quota;
}

function validateCoverage(value) {
  const coverage = expectObject(value);
  if (!COVERAGE_MODES.has(coverage.mode)) {
    throw invalidResponseError();
  }
  assertTopicId(coverage.topic_id);
  expectString(coverage.topic_label);
  expectString(coverage.notice);
  if (
    coverage.confidence !== null &&
    (
      typeof coverage.confidence !== "number" ||
      !Number.isFinite(coverage.confidence) ||
      coverage.confidence < 0 ||
      coverage.confidence > 1
    )
  ) {
    throw invalidResponseError();
  }
  if (coverage.mode === "formal") {
    assertTopicId(coverage.playbook_id);
  } else if (coverage.playbook_id !== null) {
    throw invalidResponseError();
  }
  if (
    !Array.isArray(coverage.risk_flags) ||
    coverage.risk_flags.some((flag) => !RISK_FLAGS.has(flag)) ||
    new Set(coverage.risk_flags).size !== coverage.risk_flags.length ||
    (
      coverage.mode === "emergency_guidance" &&
      coverage.risk_flags.length === 0
    )
  ) {
    throw invalidResponseError();
  }
}

function validateGuidance(value) {
  const guidance = expectObject(value);
  if (
    guidance.direct_answer !== null &&
    (
      typeof guidance.direct_answer !== "string" ||
      guidance.direct_answer.trim().length === 0 ||
      guidance.direct_answer.length > 1200
    )
  ) {
    throw invalidResponseError();
  }
  validateBoundedUniqueStringArray(guidance.evidence_now, 12, 1);
  validateBoundedUniqueStringArray(guidance.actions, 12, 1);
  validateCommunicationGuide(guidance.communication_guide);
  validateBoundedUniqueStringArray(guidance.limitations, 12, 1);
  if (
    guidance.next_question !== null &&
    (
      typeof guidance.next_question !== "string" ||
      guidance.next_question.length === 0
    )
  ) {
    throw invalidResponseError();
  }
}

function validateReply(value) {
  const reply = expectObject(value);
  expectString(reply.text);
  if (reply.text.length > 1200) {
    throw invalidResponseError();
  }
  validateBoundedUniqueStringArray(reply.suggested_actions, 3);
  validateBoundedUniqueStringArray(reply.citation_refs, 3);

  if (reply.new_case !== null) {
    const newCase = expectObject(reply.new_case);
    const scenarioValid =
      newCase.scenario_id === null ||
      (
        typeof newCase.scenario_id === "string" &&
        newCase.scenario_id.length > 0
      );
    const labelValid =
      newCase.label === null ||
      (
        typeof newCase.label === "string" &&
        newCase.label.length > 0
      );
    if (
      !scenarioValid ||
      !labelValid ||
      (newCase.scenario_id === null) !== (newCase.label === null)
    ) {
      throw invalidResponseError();
    }
  }
}

function validateTurnCombination(payload) {
  const hasPlan =
    payload.plan !== null && payload.verdict !== null;
  const hasPartialPlan =
    (payload.plan === null) !== (payload.verdict === null);
  const isGuidanceTurn = [
    "unverified_guidance",
    "emergency_guidance",
  ].includes(payload.turn_kind);
  const isSafeUnverifiedClarification =
    payload.turn_kind === "fact_collection" &&
    payload.coverage !== null &&
    payload.coverage.mode === "unverified_guidance";

  if (hasPartialPlan) {
    throw invalidResponseError();
  }
  if (isGuidanceTurn) {
    if (
      payload.coverage === null ||
      payload.guidance === null ||
      payload.coverage.mode !== payload.turn_kind ||
      hasPlan ||
      payload.reply !== null ||
      payload.questions.length > 0 ||
      payload.citations.some(
        (citation) => citation.basis_scope !== "general",
      ) ||
      !sameStringArray(
        payload.limitations,
        payload.guidance.limitations,
      ) ||
      payload.can_ask_more !==
        (payload.guidance.next_question !== null) ||
      (
        payload.turn_kind === "emergency_guidance" &&
        payload.status !== "escalate"
      )
    ) {
      throw invalidResponseError();
    }
    return;
  }
  if (
    payload.guidance !== null ||
    (
      payload.coverage !== null &&
      payload.coverage.mode !== "formal" &&
      !isSafeUnverifiedClarification
    )
  ) {
    throw invalidResponseError();
  }
  if (
    payload.turn_kind === "fact_collection" &&
    (
      hasPlan ||
      payload.citations.length > 0 ||
      (
        payload.reply !== null &&
        (
          payload.reply.new_case !== null ||
          payload.reply.citation_refs.length > 0
        )
      ) ||
      (
        payload.questions.length === 0 &&
        payload.limitations.length === 0
      )
    )
  ) {
    throw invalidResponseError();
  }
  if (
    payload.turn_kind === "initial_plan" &&
    (!hasPlan || payload.reply !== null)
  ) {
    throw invalidResponseError();
  }
  if (
    payload.turn_kind === "plan_update" &&
    (
      !hasPlan ||
      (
        payload.reply !== null &&
        payload.reply.new_case !== null
      )
    )
  ) {
    throw invalidResponseError();
  }
  if (
    payload.turn_kind === "followup_answer" &&
    (
      hasPlan ||
      payload.reply === null ||
      payload.reply.new_case !== null
    )
  ) {
    throw invalidResponseError();
  }
  if (
    payload.turn_kind === "new_case" &&
    (
      hasPlan ||
      payload.reply === null ||
      payload.reply.new_case === null
    )
  ) {
    throw invalidResponseError();
  }
  if (payload.reply !== null) {
    const publicRefs = payload.citations.map((citation) => citation.ref);
    const referencesAreInvalid = payload.turn_kind === "plan_update"
      ? payload.reply.citation_refs.some(
        (ref) => !publicRefs.includes(ref),
      )
      : (
      payload.reply.citation_refs.length !== publicRefs.length ||
      payload.reply.citation_refs.some(
        (ref, index) => ref !== publicRefs[index],
      )
      );
    if (referencesAreInvalid) {
      throw invalidResponseError();
    }
  }
}

function validatePlan(value) {
  const plan = expectObject(value);
  expectString(plan.summary);
  validateStringArray(plan.evidence_now);
  validateStringArray(plan.actions);
  expectString(plan.communication_text);
  if (
    plan.communication_guide !== null &&
    plan.communication_guide !== undefined
  ) {
    validateCommunicationGuide(plan.communication_guide);
    if (
      plan.communication_text !== plan.communication_guide.message
    ) {
      throw invalidResponseError();
    }
  }
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

function validateCommunicationGuide(value) {
  const guide = expectObject(value);
  expectString(guide.recipient);
  validateBoundedUniqueStringArray(guide.channels, 5, 1);
  expectString(guide.when_to_send);
  expectString(guide.objective);
  expectString(guide.message);
  validateBoundedUniqueStringArray(guide.after_sending, 8, 1);
  validateBoundedUniqueStringArray(guide.escalation, 8, 1);
  validateBoundedUniqueStringArray(
    guide.required_before_send,
    8,
  );
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
  if (!["case_specific", "general"].includes(citation.basis_scope)) {
    throw invalidResponseError();
  }
  if (
    citation.applicability_notice !== null &&
    (
      typeof citation.applicability_notice !== "string" ||
      citation.applicability_notice.trim().length === 0
    )
  ) {
    throw invalidResponseError();
  }
}

function validateAttachmentReview(value) {
  const item = expectObject(value);
  expectExactFields(item, REVIEW_ATTACHMENT_FIELDS);
  validateAttachmentBase(item);
  if (!ATTACHMENT_STATUSES.has(item.status)) {
    throw invalidResponseError();
  }
  validateExtractionBlocks(item.blocks, item.page_count);
  validateWarnings(item.warnings);
  validateNullableString(item.confirmed_text);
  if (
    item.error_code !== null &&
    !ATTACHMENT_ERROR_CODES.has(item.error_code)
  ) {
    throw invalidResponseError();
  }

  const hasExtraction =
    item.media_type !== null &&
    item.page_count !== null &&
    item.extraction_method !== null &&
    item.blocks.length > 0;
  if (
    item.status === "processing" &&
    (
      item.blocks.length > 0 ||
      item.confirmed_text !== null ||
      item.error_code !== null ||
      item.extraction_method !== null
    )
  ) {
    throw invalidResponseError();
  }
  if (
    item.status === "review_required" &&
    (
      !hasExtraction ||
      item.confirmed_text !== null ||
      item.error_code !== null
    )
  ) {
    throw invalidResponseError();
  }
  if (
    ["confirmed", "bound"].includes(item.status) &&
    (
      !hasExtraction ||
      !isNonBlankString(item.confirmed_text) ||
      item.error_code !== null
    )
  ) {
    throw invalidResponseError();
  }
  if (
    item.status === "failed" &&
    (
      !ATTACHMENT_ERROR_CODES.has(item.error_code) ||
      item.blocks.length > 0 ||
      item.confirmed_text !== null
    )
  ) {
    throw invalidResponseError();
  }
  return item;
}

function validateRequestedAttachment(
  value,
  attachmentId,
  expectedStatus = null,
) {
  const item = validateAttachmentReview(value);
  if (
    item.id.toLowerCase() !== attachmentId.toLowerCase() ||
    (expectedStatus !== null && item.status !== expectedStatus)
  ) {
    throw invalidResponseError();
  }
  return item;
}

function validateAttachmentTurn(value) {
  const item = expectObject(value);
  expectExactFields(item, TURN_ATTACHMENT_FIELDS);
  validateAttachmentBase(item);
  if (
    item.status !== "bound" ||
    item.media_type === null ||
    item.page_count === null ||
    item.extraction_method === null ||
    !isNonBlankString(item.confirmed_text)
  ) {
    throw invalidResponseError();
  }
  validateWarnings(item.warnings);
  return item;
}

function validateAttachmentBase(item) {
  assertUuid(item.id);
  if (
    !isNonBlankString(item.original_name) ||
    item.original_name.length > 255 ||
    /[\u0000-\u001f\u007f-\u009f]/u.test(item.original_name) ||
    !Number.isInteger(item.size_bytes) ||
    item.size_bytes < 0 ||
    (
      item.media_type !== null &&
      !ATTACHMENT_MEDIA_TYPES.has(item.media_type)
    ) ||
    (
      item.page_count !== null &&
      (
        !Number.isInteger(item.page_count) ||
        item.page_count < 1
      )
    ) ||
    (
      item.extraction_method !== null &&
      !EXTRACTION_METHODS.has(item.extraction_method)
    )
  ) {
    throw invalidResponseError();
  }
}

function validateExtractionBlocks(blocks, pageCount) {
  if (!Array.isArray(blocks)) {
    throw invalidResponseError();
  }
  const positions = new Set();
  for (const value of blocks) {
    const block = expectObject(value);
    expectExactFields(block, EXTRACTION_BLOCK_FIELDS);
    if (
      !Number.isInteger(block.page_number) ||
      block.page_number < 1 ||
      !Number.isInteger(block.block_index) ||
      block.block_index < 0 ||
      !isNonBlankString(block.text) ||
      typeof block.confidence !== "number" ||
      !Number.isFinite(block.confidence) ||
      block.confidence < 0 ||
      block.confidence > 1 ||
      (
        pageCount !== null &&
        block.page_number > pageCount
      )
    ) {
      throw invalidResponseError();
    }
    const position = `${block.page_number}:${block.block_index}`;
    if (positions.has(position)) {
      throw invalidResponseError();
    }
    positions.add(position);
  }
}

function validateWarnings(warnings) {
  if (
    !Array.isArray(warnings) ||
    warnings.some(
      (warning) =>
        typeof warning !== "string" ||
        !WARNING_CODE_PATTERN.test(warning),
    ) ||
    new Set(warnings).size !== warnings.length
  ) {
    throw invalidResponseError();
  }
}

function validateAttachmentIds(values) {
  if (
    !Array.isArray(values) ||
    values.length > 3 ||
    new Set(values).size !== values.length
  ) {
    throw invalidRequestError("本轮最多选择 3 个不同附件。");
  }
  try {
    values.forEach(assertUuid);
  } catch {
    throw invalidRequestError("附件状态无效，请重新上传。");
  }
}

function expectExactFields(value, fields) {
  const keys = Object.keys(value);
  if (
    keys.length !== fields.size ||
    keys.some((key) => !fields.has(key))
  ) {
    throw invalidResponseError();
  }
}

function validateNullableString(value) {
  if (value !== null && !isNonBlankString(value)) {
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

function validateBoundedUniqueStringArray(
  value,
  maximum,
  minimum = 0,
) {
  validateStringArray(value);
  if (
    value.length < minimum ||
    value.length > maximum ||
    value.some((item) => item.trim().length === 0) ||
    new Set(value).size !== value.length
  ) {
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

function assertTopicId(value) {
  if (
    typeof value !== "string" ||
    !/^[a-z][a-z0-9_]{1,99}$/.test(value)
  ) {
    throw invalidResponseError();
  }
}

function sameStringArray(left, right) {
  return (
    left.length === right.length &&
    left.every((item, index) => item === right[index])
  );
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonBlankString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function attachmentErrorMessage(code) {
  const messages = {
    attachment_type_unsupported:
      "仅支持 PDF、PNG、JPG 或 JPEG 文件。",
    attachment_type_mismatch:
      "文件内容与格式不一致，请重新选择原始文件。",
    attachment_name_invalid:
      "文件名无效，请重命名后重新选择。",
    attachment_pdf_encrypted:
      "暂不支持加密 PDF，请移除密码后重试。",
    attachment_corrupt:
      "文件无法读取，请重新导出或选择其他文件。",
    attachment_text_empty:
      "未识别到可核对文字，请选择更清晰的文件。",
    attachment_too_large:
      "文件过大，请压缩或拆分后重试。",
    attachment_page_limit_exceeded:
      "PDF 页数过多，请拆分后重试。",
    attachment_pixel_limit_exceeded:
      "图片尺寸过大，请缩小后重试。",
    attachment_extracted_text_too_long:
      "文件文字过多，请拆分后重试。",
    attachment_extraction_timeout:
      "文件处理超时，请缩小文件或稍后重试。",
    attachment_not_found:
      "附件不存在或已过期，请重新上传。",
    attachment_not_reviewable:
      "当前附件状态不可核对，请刷新后重试。",
    attachment_not_confirmed:
      "附件尚未确认，请先核对文字。",
    attachment_already_bound:
      "附件已用于其他咨询，请重新上传。",
    attachment_count_exceeded:
      "本轮附件数量过多，请移除部分附件。",
    attachment_context_too_long:
      "本轮附件文字总量过多，请减少内容后重试。",
    attachment_service_unavailable:
      "本地文字提取暂时不可用，仍可继续文字咨询。",
  };
  return Object.hasOwn(messages, code) ? messages[code] : null;
}

function invalidRequestError(userMessage) {
  return new ApiError({
    code: "invalid_request",
    status: 0,
    message: "Request shape is invalid",
    userMessage,
  });
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

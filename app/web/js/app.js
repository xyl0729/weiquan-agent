import {
  acceptPrivacy,
  ApiError,
  confirmAttachment,
  consult,
  deleteAttachment,
  deleteSession,
  getAttachment,
  getCurrentAccount,
  getHealth,
  getRuntimeConfig,
  getSession,
  listSessions,
  refreshCsrfToken,
  startTrial,
  trialConsult,
  uploadAttachment,
} from "./api.js";
import {
  consumeAuthFragment,
  createAuthController,
  discardAuthFragment,
} from "./auth.js";
import {
  hasRegisteredAccount,
  hasWorkspaceAccess,
} from "./capabilities.js";
import {
  createCaptchaController,
  loadCaptchaConfig,
} from "./captcha.js";
import {
  createPrivacyController,
  PrivacyCancelledError,
} from "./privacy.js";
import { createRenderer } from "./render.js";
import {
  clearAuthenticatedState,
  createAttachmentGroup,
  createStore,
  readAttachmentDraftIds,
  readRememberedSessionId,
  rememberAttachmentDraftIds,
  rememberCurrentSessionId,
} from "./state.js";

const MAX_REGISTERED_MESSAGE_LENGTH = 4000;
const MAX_TRIAL_MESSAGE_LENGTH = 3000;
const MAX_ATTACHMENT_COUNT = 3;
const MAX_ATTACHMENT_CONTEXT_LENGTH = 12000;
const MAX_FILE_BYTES = 10 * 1024 * 1024;
const store = createStore();
let consultGeneration = 0;
let detailGeneration = 0;
let historyGeneration = 0;
let attachmentKeyGeneration = 0;
let toastGeneration = 0;
let toastTimer = null;
let identityActionPending = false;
const attachmentRestoreGenerations = {
  new: 0,
  case: 0,
};
const retryFiles = new Map();
const replacementAttachmentKeys = {
  new: null,
  case: null,
};
let reviewReturnFocus = null;

const elements = {
  newForm: requiredElement("new-form"),
  newMessage: requiredElement("new-message"),
  caseForm: requiredElement("case-form"),
  caseMessage: requiredElement("case-message"),
  newSession: requiredElement("new-session"),
  historyToggle: requiredElement("history-toggle"),
  historyClose: requiredElement("history-close"),
  historyRetry: requiredElement("history-retry"),
  historyPanel: requiredElement("history-panel"),
  drawerBackdrop: requiredElement("drawer-backdrop"),
  newSend: requiredElement("new-send"),
  caseSend: requiredElement("case-send"),
  privacyPolicyOpen: requiredElement("privacy-policy-open"),
  trialPolicyOpen: requiredElement("trial-policy-open"),
  trialLogin: requiredElement("trial-login"),
  trialRegister: requiredElement("trial-register"),
  trialAgain: requiredElement("trial-again"),
  newAttachmentInput: requiredElement("new-attachment-input"),
  newAttachmentTrigger: requiredElement("new-attachment-trigger"),
  caseAttachmentInput: requiredElement("case-attachment-input"),
  caseAttachmentTrigger: requiredElement("case-attachment-trigger"),
  thread: requiredElement("thread"),
  deleteDialog: requiredElement("delete-dialog"),
  deleteDescription: requiredElement("delete-dialog-description"),
  deleteConfirm: requiredElement("delete-confirm"),
  attachmentReviewDialog: requiredElement("attachment-review-dialog"),
  attachmentReviewForm: requiredElement("attachment-review-form"),
  attachmentReviewPages: requiredElement("attachment-review-pages"),
  attachmentReviewCount: requiredElement("attachment-review-count"),
  attachmentReviewClose: requiredElement("attachment-review-close"),
  attachmentReviewCancel: requiredElement("attachment-review-cancel"),
  attachmentReviewConfirm: requiredElement(
    "attachment-review-confirm",
  ),
};

const registrationCaptcha = createCaptchaController({
  slotId: "captcha-slot",
  buttonId: "auth-captcha-button",
  statusId: "auth-captcha-status",
});
const anonymousCaptcha = createCaptchaController({
  slotId: "trial-captcha-slot",
  buttonId: "trial-captcha-button",
  statusId: "trial-captcha-status",
});
const privacy = createPrivacyController({
  captcha: anonymousCaptcha,
});
const auth = createAuthController({
  captcha: registrationCaptcha,
  privacy,
  getIdentity: () => store.getState().identity,
  onAuthenticated: activateAuthenticatedAccount,
  onLoggedOut: resetAfterLogout,
  onStatus: setIdentityStatus,
});
const render = createRenderer({
  continueAsNew,
  openSession,
  removeAttachment,
  replaceAttachment,
  retryAttachment,
  requestDelete,
  retryCase,
  reviewAttachment,
  startNew,
});
store.subscribe(render);
bindEvents();
void bootstrap();

function bindEvents() {
  elements.newMessage.addEventListener("input", (event) => {
    updateDraft("new", event.currentTarget.value);
  });
  elements.caseMessage.addEventListener("input", (event) => {
    updateDraft("case", event.currentTarget.value);
  });
  elements.newForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitConsultation("new");
  });
  elements.caseForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitConsultation("case");
  });
  bindEnterToSubmit(elements.newMessage, elements.newForm);
  bindEnterToSubmit(elements.caseMessage, elements.caseForm);

  for (const starter of document.querySelectorAll("[data-starter]")) {
    starter.addEventListener("click", () => {
      const message = starter.dataset.starter || "";
      store.setState((state) => ({
        ...state,
        draftNew: message,
        inputErrors: { ...state.inputErrors, new: "" },
      }));
      elements.newMessage.focus();
      elements.newMessage.setSelectionRange(
        message.length,
        message.length,
      );
    });
  }

  elements.newSession.addEventListener("click", startNew);
  elements.historyToggle.addEventListener("click", () => {
    setHistoryOpen(true);
    requestAnimationFrame(() => elements.newSession.focus());
  });
  elements.historyClose.addEventListener("click", () => {
    setHistoryOpen(false);
    elements.historyToggle.focus();
  });
  elements.drawerBackdrop.addEventListener("click", () => {
    setHistoryOpen(false);
  });
  elements.historyRetry.addEventListener("click", () => {
    void refreshHistory();
  });
  elements.privacyPolicyOpen.addEventListener("click", () => {
    void privacy.showPolicy(elements.privacyPolicyOpen).catch(() => {});
  });
  elements.trialPolicyOpen.addEventListener("click", () => {
    void privacy.showPolicy(elements.trialPolicyOpen).catch(() => {});
  });
  elements.trialLogin.addEventListener("click", () => {
    auth.open("login");
  });
  elements.trialRegister.addEventListener("click", () => {
    auth.open("register");
  });
  elements.trialAgain.addEventListener("click", startNew);

  for (const source of ["new", "case"]) {
    const input = attachmentInput(source);
    const trigger = attachmentTrigger(source);
    trigger.addEventListener("click", () => {
      if (!trigger.disabled) {
        replacementAttachmentKeys[source] = null;
        input.multiple = true;
        input.click();
      }
    });
    input.addEventListener("change", (event) => {
      const files = [...(event.currentTarget.files || [])];
      const replacementKey = replacementAttachmentKeys[source];
      replacementAttachmentKeys[source] = null;
      event.currentTarget.multiple = true;
      event.currentTarget.value = "";
      void handleFileSelection(source, files, replacementKey);
    });
    input.addEventListener("cancel", () => {
      replacementAttachmentKeys[source] = null;
      input.multiple = true;
    });
  }

  elements.attachmentReviewForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void saveAttachmentReview();
  });
  elements.attachmentReviewPages.addEventListener("input", () => {
    updateReviewCharacterCount();
  });
  elements.attachmentReviewClose.addEventListener("click", closeReview);
  elements.attachmentReviewCancel.addEventListener("click", closeReview);
  elements.attachmentReviewDialog.addEventListener("cancel", (event) => {
    if (store.getState().attachments.review?.saving) {
      event.preventDefault();
      return;
    }
    clearReviewState();
  });
  elements.attachmentReviewDialog.addEventListener("close", () => {
    if (store.getState().attachments.review?.saving) {
      return;
    }
    clearReviewState();
    const target = reviewReturnFocus;
    reviewReturnFocus = null;
    if (target?.isConnected) {
      target.focus();
    }
  });
  elements.deleteConfirm.addEventListener("click", (event) => {
    event.preventDefault();
    void confirmDelete();
  });
  elements.deleteDialog.addEventListener("close", () => {
    if (!store.getState().busy.delete) {
      store.setState((state) => ({
        ...state,
        deleteTarget: null,
      }));
    }
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && store.getState().historyOpen) {
      setHistoryOpen(false);
      elements.historyToggle.focus();
    }
  });
}

async function bootstrap() {
  let runtimeConfig;
  try {
    runtimeConfig = await getRuntimeConfig();
  } catch (error) {
    activateUnavailableIdentity(error);
    return;
  }

  if (runtimeConfig.identity_mode === "local_full_test") {
    discardAuthFragment();
    await Promise.allSettled([
      loadHealth(),
      activateLocalWorkspace(),
    ]);
    return;
  }

  const authFragment = consumeAuthFragment();
  const [, , captchaResult] = await Promise.allSettled([
    loadHealth(),
    privacy.load(),
    loadCaptchaConfig(),
  ]);
  if (captchaResult.status === "fulfilled") {
    await Promise.allSettled([
      registrationCaptcha.configure(captchaResult.value),
      anonymousCaptcha.configure(captchaResult.value),
    ]);
  }
  await restoreIdentity();
  await auth.handleFragment(authFragment);
}

function activateUnavailableIdentity(error) {
  discardAuthFragment();
  resetWorkState({ status: "unavailable" });
  store.setState((state) => ({
    ...state,
    service: {
      status: "error",
      label: "运行配置不可用",
    },
    attachments: {
      ...state.attachments,
      ocr: {
        status: "error",
        label: "无法确认运行模式，工作区已停用。",
      },
    },
  }));
  showToast(safeErrorMessage(error), "error");
}

async function restoreIdentity() {
  try {
    const projection = await getCurrentAccount();
    await activateAuthenticatedAccount(projection);
    return;
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) {
      showToast(safeErrorMessage(error), "error");
    }
  }
  await restoreAnonymousIdentity();
}

async function activateAuthenticatedAccount(projection) {
  await refreshCsrfToken();
  const status =
    projection.user.status === "active"
      ? "authenticated"
      : projection.user.status;
  if (status !== "authenticated") {
    clearAuthenticatedState();
    resetWorkState({
      status,
      user: projection.user,
      quota: projection.quota,
      privacyVersion: projection.privacy_version,
      privacyAcceptanceRequired:
        projection.privacy_acceptance_required,
    });
    return;
  }

  await activateWorkspace({
    status: "authenticated",
    user: projection.user,
    quota: projection.quota,
    privacyVersion: projection.privacy_version,
    privacyAcceptanceRequired:
      projection.privacy_acceptance_required,
  });
}

async function activateLocalWorkspace() {
  await activateWorkspace({
    status: "local",
    user: null,
    quota: null,
    privacyVersion: "",
    privacyAcceptanceRequired: false,
  });
}

async function activateWorkspace(identity) {
  const rememberedSessionId = readRememberedSessionId();
  consultGeneration += 1;
  detailGeneration += 1;
  historyGeneration += 1;
  attachmentRestoreGenerations.new += 1;
  attachmentRestoreGenerations.case += 1;
  retryFiles.clear();
  store.setState((state) => ({
    ...state,
    view: "new",
    historyOpen: false,
    identity: {
      status: identity.status,
      user: identity.user,
      pendingEmail: "",
      trialIdentityId: null,
      quota: identity.quota,
      privacyVersion: identity.privacyVersion,
      privacyAcceptanceRequired:
        identity.privacyAcceptanceRequired,
    },
    history: {
      status: "loading",
      items: [],
      refreshing: false,
      error: null,
    },
    currentSessionId: rememberedSessionId,
    session: null,
    turns: [],
    draftCase: "",
    attachments: {
      ...state.attachments,
      new: createAttachmentGroup(null),
      case: createAttachmentGroup(null, false),
      review: null,
    },
    busy: {
      consult: false,
      session: false,
      delete: false,
    },
    inputErrors: { new: "", case: "" },
    caseError: null,
    sessionExpired: false,
    deleteTarget: null,
  }));
  await Promise.allSettled([
    refreshHistory({ restoreId: rememberedSessionId }),
    restoreAttachmentGroup("new", null),
  ]);
}

function setIdentityStatus(status, patch = {}) {
  store.setState((state) => ({
    ...state,
    identity: {
      ...state.identity,
      status,
      ...patch,
    },
  }));
}

async function resetAfterLogout() {
  clearAuthenticatedState();
  consultGeneration += 1;
  detailGeneration += 1;
  historyGeneration += 1;
  attachmentRestoreGenerations.new += 1;
  attachmentRestoreGenerations.case += 1;
  retryFiles.clear();
  resetWorkState({ status: "loading" });
  await restoreAnonymousIdentity();
}

async function restoreAnonymousIdentity({
  status = "trial",
  pendingEmail = "",
} = {}) {
  let trial = null;
  try {
    trial = await startTrial();
  } catch (error) {
    if (
      !(error instanceof ApiError) ||
      ![
        "privacy_acceptance_required",
        "trial_identity_required",
      ].includes(error.code)
    ) {
      showToast(safeErrorMessage(error), "error");
    }
  }
  resetWorkState({
    status,
    user: null,
    pendingEmail,
    trialIdentityId: trial?.identity_id || null,
    quota: trial?.quota || null,
    privacyVersion: privacy.getPolicy()?.version || "",
    privacyAcceptanceRequired: false,
  });
}

function resetWorkState(identity) {
  store.setState((state) => ({
    ...state,
    view: "new",
    historyOpen: false,
    identity: {
      status: identity.status,
      user: identity.user || null,
      pendingEmail: identity.pendingEmail || "",
      trialIdentityId: identity.trialIdentityId || null,
      quota: identity.quota || null,
      privacyVersion: identity.privacyVersion || "",
      privacyAcceptanceRequired: Boolean(
        identity.privacyAcceptanceRequired,
      ),
    },
    history: {
      status: "idle",
      items: [],
      refreshing: false,
      error: null,
    },
    currentSessionId: null,
    session: null,
    turns: [],
    draftNew: "",
    draftCase: "",
    attachments: {
      ...state.attachments,
      new: createAttachmentGroup(null, false),
      case: createAttachmentGroup(null, false),
      review: null,
    },
    busy: {
      consult: false,
      session: false,
      delete: false,
    },
    inputErrors: { new: "", case: "" },
    caseError: null,
    sessionExpired: false,
    deleteTarget: null,
  }));
}

async function loadHealth() {
  store.setState((state) => ({
    ...state,
    service: { status: "loading", label: "正在连接" },
  }));
  try {
    const health = await getHealth();
    const available = health.status === "ok";
    const ocrReady = health.checks.ocr === "ok";
    store.setState((state) => ({
      ...state,
      service: {
        status: available ? "ok" : "error",
        label: available ? "本地服务可用" : "部分服务不可用",
      },
      attachments: {
        ...state.attachments,
        ocr: {
          status: ocrReady ? "ready" : "error",
          label: ocrReady
            ? "本地文字提取可用"
            : "本地文字提取暂时不可用，仍可直接发送文字咨询。",
        },
      },
    }));
  } catch {
    store.setState((state) => ({
      ...state,
      service: { status: "error", label: "连接失败" },
      attachments: {
        ...state.attachments,
        ocr: {
          status: "error",
          label: "无法检查本地文字提取，仍可直接发送文字咨询。",
        },
      },
    }));
  }
}

async function restoreAttachmentGroup(source, scopeId) {
  if (!hasWorkspaceAccess(store.getState().identity)) {
    return;
  }
  const generation = ++attachmentRestoreGenerations[source];
  const draftIds = readAttachmentDraftIds(scopeId);
  updateAttachmentGroup(source, scopeId, (group) => ({
    ...group,
    draftIds,
    restoredDraftIds: [],
    items: [],
    restored: draftIds.length === 0,
    restoring: draftIds.length > 0,
    uploading: false,
    working: false,
    error: "",
  }));
  if (draftIds.length === 0) {
    return;
  }

  const restored = [];
  let discardedCount = 0;
  for (const attachmentId of draftIds) {
    try {
      const item = await getAttachment(attachmentId);
      if (
        generation !== attachmentRestoreGenerations[source] ||
        !isCurrentAttachmentScope(source, scopeId)
      ) {
        return;
      }
      if (item.status !== "bound") {
        restored.push(attachmentForDisplay(item));
      } else {
        discardedCount += 1;
      }
    } catch (error) {
      if (
        generation !== attachmentRestoreGenerations[source] ||
        !isCurrentAttachmentScope(source, scopeId)
      ) {
        return;
      }
      if (isDiscardedAttachmentError(error)) {
        discardedCount += 1;
        continue;
      }
      restored.push({
        id: attachmentId,
        status: "failed",
        original_name: "未恢复的材料",
        media_type: null,
        size_bytes: 0,
        page_count: null,
        extraction_method: null,
        blocks: [],
        warnings: [],
        confirmed_text: null,
        error_code: null,
        recovery_failed: true,
        ui_error: safeErrorMessage(error),
      });
    }
  }

  if (
    generation !== attachmentRestoreGenerations[source] ||
    !isCurrentAttachmentScope(source, scopeId)
  ) {
    return;
  }
  updateAttachmentGroup(source, scopeId, (group) =>
    groupWithItems(
      group,
      mergeRestoredAttachmentItems(restored, group.items),
      {
        restored: true,
        restoring: false,
        restoredDraftIds: attachmentDraftIds(restored),
        error:
          discardedCount > 0
            ? "部分材料已过期或已使用，已移除，请重新添加。"
            : "",
      },
    ),
  );
  persistAttachmentGroup(source, scopeId);
}

async function handleFileSelection(
  source,
  files,
  replacementKey = null,
) {
  if (!hasWorkspaceAccess(store.getState().identity)) {
    return;
  }
  if (!Array.isArray(files) || files.length === 0) {
    return;
  }
  const state = store.getState();
  const group = state.attachments[source];
  if (
    state.busy.consult ||
    state.busy.session ||
    group.uploading ||
    group.restoring ||
    group.working ||
    state.attachments.ocr.status !== "ready" ||
    (source === "case" && state.sessionExpired)
  ) {
    return;
  }

  if (replacementKey !== null) {
    if (files.length !== 1) {
      setAttachmentGroupError(
        source,
        group.scopeId,
        "重新选择材料时一次只能选择 1 个文件。",
      );
      return;
    }
    await replaceSelectedAttachment(
      source,
      group.scopeId,
      replacementKey,
      files[0],
    );
    return;
  }

  const available = MAX_ATTACHMENT_COUNT - group.items.length;
  if (files.length > available) {
    setAttachmentGroupError(
      source,
      group.scopeId,
      `本轮最多添加 ${MAX_ATTACHMENT_COUNT} 个材料，当前还可添加 ${Math.max(0, available)} 个。`,
    );
    return;
  }

  const scopeId = group.scopeId;
  updateAttachmentGroup(source, scopeId, (current) => ({
    ...current,
    uploading: true,
    error: "",
  }));
  try {
    for (const file of files) {
      if (!isCurrentAttachmentScope(source, scopeId)) {
        return;
      }
      await uploadFileIntoGroup(source, scopeId, file);
    }
  } finally {
    updateAttachmentGroup(source, scopeId, (current) => ({
      ...current,
      uploading: false,
    }));
  }
}

async function uploadFileIntoGroup(
  source,
  scopeId,
  file,
  position = null,
) {
  const key = `attachment-${++attachmentKeyGeneration}`;
  const placeholder = {
    ui_key: key,
    status: "processing",
    original_name: String(file?.name || "未命名材料"),
    media_type:
      typeof file?.type === "string" && file.type
        ? file.type
        : null,
    size_bytes: Number.isInteger(file?.size) ? file.size : 0,
    page_count: null,
    extraction_method: null,
    blocks: [],
    warnings: [],
    confirmed_text: null,
    error_code: null,
  };
  updateAttachmentGroup(source, scopeId, (group) => {
    const items = [...group.items];
    const target = Number.isInteger(position)
      ? Math.min(Math.max(position, 0), items.length)
      : items.length;
    items.splice(target, 0, placeholder);
    return groupWithItems(group, items, { error: "" });
  });

  const validationMessage = validateSelectedFile(file);
  if (validationMessage !== null) {
    updateAttachmentGroup(source, scopeId, (group) =>
      replaceGroupItem(group, key, {
        ...placeholder,
        status: "failed",
        ui_error: validationMessage,
        can_retry: false,
      }),
    );
    return;
  }

  try {
    const uploaded = await uploadAttachment(file);
    if (!isCurrentAttachmentScope(source, scopeId)) {
      rememberDetachedAttachment(scopeId, uploaded);
      return;
    }
    const item = attachmentForDisplay(uploaded, true);
    retryFiles.delete(key);
    if (item.can_retry) {
      retryFiles.set(item.id, file);
    }
    updateAttachmentGroup(source, scopeId, (group) =>
      replaceGroupItem(group, key, item),
    );
    persistAttachmentGroup(source, scopeId);
  } catch (error) {
    if (!isCurrentAttachmentScope(source, scopeId)) {
      retryFiles.delete(key);
      return;
    }
    const retryable =
      error instanceof ApiError ? error.retryable : true;
    if (retryable) {
      retryFiles.set(key, file);
    } else {
      retryFiles.delete(key);
    }
    updateAttachmentGroup(source, scopeId, (group) =>
      replaceGroupItem(group, key, {
        ...placeholder,
        status: "failed",
        ui_error: safeErrorMessage(error),
        can_retry: retryable,
      }),
    );
  }
}

function reviewAttachment(source, key, returnFocus) {
  const state = store.getState();
  if (!hasWorkspaceAccess(state.identity)) {
    return;
  }
  const group = state.attachments[source];
  const item = group.items.find(
    (candidate) => attachmentItemKey(candidate) === key,
  );
  if (
    !item?.id ||
    !["review_required", "confirmed"].includes(item.status) ||
    state.busy.consult ||
    group.uploading ||
    group.restoring ||
    group.working
  ) {
    return;
  }

  reviewReturnFocus =
    returnFocus instanceof HTMLElement
      ? returnFocus
      : attachmentTrigger(source);
  store.setState((current) => ({
    ...current,
    attachments: {
      ...current.attachments,
      review: {
        source,
        key,
        scopeId: group.scopeId,
        item,
        saving: false,
        error: "",
      },
    },
  }));
  if (!elements.attachmentReviewDialog.open) {
    elements.attachmentReviewDialog.showModal();
  }
  requestAnimationFrame(() => {
    elements.attachmentReviewPages
      .querySelector("[data-review-block]")
      ?.focus();
    updateReviewCharacterCount();
  });
}

async function saveAttachmentReview() {
  if (!hasWorkspaceAccess(store.getState().identity)) {
    return;
  }
  const review = store.getState().attachments.review;
  if (!review || review.saving || store.getState().busy.consult) {
    return;
  }
  const confirmedText = readReviewText();
  const totalLength = reviewTotalLength(review, confirmedText);
  updateReviewCharacterCount();
  if (!confirmedText) {
    setReviewError("确认文字不能为空。");
    return;
  }
  if (totalLength > MAX_ATTACHMENT_CONTEXT_LENGTH) {
    setReviewError(
      `本轮材料确认文字不能超过 ${MAX_ATTACHMENT_CONTEXT_LENGTH} 个字符。`,
    );
    return;
  }

  let started = false;
  store.setState((state) => {
    if (!sameReviewIdentity(state.attachments.review, review)) {
      return state;
    }
    started = true;
    return {
      ...state,
      attachments: {
        ...state.attachments,
        review: {
          ...state.attachments.review,
          saving: true,
          error: "",
        },
      },
    };
  });
  if (!started) {
    return;
  }

  try {
    const confirmed = await confirmAttachment(
      review.item.id,
      confirmedText,
    );
    const nextItem = {
      ...attachmentForDisplay(confirmed),
      ...(review.item.ui_key
        ? { ui_key: review.item.ui_key }
        : {}),
    };
    let applied = false;
    store.setState((state) => {
      const group = state.attachments[review.source];
      const currentItem = group.items.find(
        (item) => attachmentItemKey(item) === review.key,
      );
      if (
        !sameReviewIdentity(state.attachments.review, review) ||
        group.scopeId !== review.scopeId ||
        currentItem?.id !== review.item.id
      ) {
        return state;
      }
      applied = true;
      return {
        ...state,
        attachments: {
          ...state.attachments,
          [review.source]: replaceGroupItem(
            group,
            review.key,
            nextItem,
          ),
          review: null,
        },
      };
    });
    if (!applied) {
      return;
    }
    persistAttachmentGroup(
      review.source,
      review.scopeId,
    );
    if (elements.attachmentReviewDialog.open) {
      elements.attachmentReviewDialog.close();
    }
  } catch (error) {
    store.setState((state) => {
      if (!sameReviewIdentity(state.attachments.review, review)) {
        return state;
      }
      return {
        ...state,
        attachments: {
          ...state.attachments,
          review: {
            ...state.attachments.review,
            saving: false,
            error: safeErrorMessage(error),
          },
        },
      };
    });
  }
}

function closeReview() {
  if (store.getState().attachments.review?.saving) {
    return;
  }
  if (elements.attachmentReviewDialog.open) {
    elements.attachmentReviewDialog.close();
  } else {
    clearReviewState();
  }
}

function clearReviewState() {
  if (!store.getState().attachments.review) {
    return;
  }
  store.setState((state) => ({
    ...state,
    attachments: {
      ...state.attachments,
      review: null,
    },
  }));
}

function updateReviewCharacterCount() {
  const review = store.getState().attachments.review;
  if (!review) {
    return;
  }
  const confirmedText = readReviewText();
  const length = reviewTotalLength(review, confirmedText);
  elements.attachmentReviewCount.textContent =
    `本轮确认文字 ${length} / ${MAX_ATTACHMENT_CONTEXT_LENGTH}`;
  elements.attachmentReviewCount.dataset.overLimit = String(
    length > MAX_ATTACHMENT_CONTEXT_LENGTH,
  );
  elements.attachmentReviewConfirm.disabled =
    review.saving ||
    store.getState().busy.consult ||
    !confirmedText ||
    length > MAX_ATTACHMENT_CONTEXT_LENGTH;
}

async function removeAttachment(source, key) {
  const state = store.getState();
  if (!hasWorkspaceAccess(state.identity)) {
    return;
  }
  const group = state.attachments[source];
  const item = group.items.find(
    (candidate) => attachmentItemKey(candidate) === key,
  );
  if (
    !item ||
    state.busy.consult ||
    group.uploading ||
    group.restoring ||
    group.working
  ) {
    return;
  }
  const scopeId = group.scopeId;
  updateAttachmentGroup(source, scopeId, (current) => ({
    ...current,
    working: true,
    error: "",
  }));
  try {
    if (item.id) {
      try {
        await deleteAttachment(item.id);
      } catch (error) {
        if (!isDiscardedAttachmentError(error)) {
          throw error;
        }
      }
    }
    retryFiles.delete(key);
    if (item.id) {
      retryFiles.delete(item.id);
    }
    updateAttachmentGroup(source, scopeId, (current) =>
      groupWithItems(
        current,
        current.items.filter(
          (candidate) => attachmentItemKey(candidate) !== key,
        ),
        { working: false, error: "" },
      ),
    );
    persistAttachmentGroup(source, scopeId);
    if (store.getState().attachments.review?.key === key) {
      closeReview();
    }
  } catch (error) {
    updateAttachmentGroup(source, scopeId, (current) => ({
      ...current,
      working: false,
      error: safeErrorMessage(error),
    }));
  }
}

function replaceAttachment(source, key) {
  const state = store.getState();
  if (!hasWorkspaceAccess(state.identity)) {
    return;
  }
  const group = state.attachments[source];
  const exists = group.items.some(
    (item) => attachmentItemKey(item) === key,
  );
  if (
    !exists ||
    state.busy.consult ||
    group.uploading ||
    group.restoring ||
    group.working
  ) {
    return;
  }
  const input = attachmentInput(source);
  replacementAttachmentKeys[source] = key;
  input.multiple = false;
  input.click();
}

async function replaceSelectedAttachment(
  source,
  scopeId,
  key,
  file,
) {
  const group = store.getState().attachments[source];
  const index = group.items.findIndex(
    (item) => attachmentItemKey(item) === key,
  );
  if (group.scopeId !== scopeId || index < 0) {
    return;
  }
  const validationMessage = validateSelectedFile(file);
  if (validationMessage !== null) {
    setAttachmentGroupError(
      source,
      scopeId,
      validationMessage,
    );
    return;
  }

  const item = group.items[index];
  updateAttachmentGroup(source, scopeId, (current) => ({
    ...current,
    uploading: true,
    error: "",
  }));
  try {
    if (item.id) {
      try {
        await deleteAttachment(item.id);
      } catch (error) {
        if (!isDiscardedAttachmentError(error)) {
          throw error;
        }
      }
    }
    retryFiles.delete(key);
    if (item.id) {
      retryFiles.delete(item.id);
    }
    updateAttachmentGroup(source, scopeId, (current) =>
      groupWithItems(
        current,
        current.items.filter(
          (candidate) => attachmentItemKey(candidate) !== key,
        ),
      ),
    );
    persistAttachmentGroup(source, scopeId);
    await uploadFileIntoGroup(source, scopeId, file, index);
  } catch (error) {
    setAttachmentGroupError(
      source,
      scopeId,
      safeErrorMessage(error),
    );
  } finally {
    updateAttachmentGroup(source, scopeId, (current) => ({
      ...current,
      uploading: false,
    }));
  }
}

async function retryAttachment(source, key) {
  const state = store.getState();
  if (!hasWorkspaceAccess(state.identity)) {
    return;
  }
  const group = state.attachments[source];
  const item = group.items.find(
    (candidate) => attachmentItemKey(candidate) === key,
  );
  if (
    !item ||
    state.busy.consult ||
    group.uploading ||
    group.restoring ||
    group.working
  ) {
    return;
  }
  const scopeId = group.scopeId;

  if (item.recovery_failed && item.id) {
    updateAttachmentGroup(source, scopeId, (current) => ({
      ...current,
      working: true,
      error: "",
    }));
    try {
      const restored = await getAttachment(item.id);
      if (restored.status === "bound") {
        updateAttachmentGroup(source, scopeId, (current) =>
          groupWithItems(
            current,
            current.items.filter(
              (candidate) =>
                attachmentItemKey(candidate) !== key,
            ),
            { working: false },
          ),
        );
      } else {
        updateAttachmentGroup(source, scopeId, (current) =>
          replaceGroupItem(
            { ...current, working: false },
            key,
            attachmentForDisplay(restored),
          ),
        );
      }
      persistAttachmentGroup(source, scopeId);
    } catch (error) {
      if (isDiscardedAttachmentError(error)) {
        updateAttachmentGroup(source, scopeId, (current) =>
          groupWithItems(
            current,
            current.items.filter(
              (candidate) =>
                attachmentItemKey(candidate) !== key,
            ),
            { working: false },
          ),
        );
        persistAttachmentGroup(source, scopeId);
      } else {
        updateAttachmentGroup(source, scopeId, (current) => ({
          ...current,
          working: false,
          error: safeErrorMessage(error),
        }));
      }
    }
    return;
  }

  const file = retryFiles.get(key) ||
    (item.id ? retryFiles.get(item.id) : null);
  if (!file) {
    replaceAttachment(source, key);
    return;
  }
  const index = group.items.findIndex(
    (candidate) => attachmentItemKey(candidate) === key,
  );
  updateAttachmentGroup(source, scopeId, (current) => ({
    ...current,
    uploading: true,
    error: "",
  }));
  try {
    if (item.id) {
      try {
        await deleteAttachment(item.id);
      } catch (error) {
        if (!isDiscardedAttachmentError(error)) {
          throw error;
        }
      }
    }
    retryFiles.delete(key);
    if (item.id) {
      retryFiles.delete(item.id);
    }
    updateAttachmentGroup(source, scopeId, (current) =>
      groupWithItems(
        current,
        current.items.filter(
          (candidate) => attachmentItemKey(candidate) !== key,
        ),
      ),
    );
    persistAttachmentGroup(source, scopeId);
    await uploadFileIntoGroup(source, scopeId, file, index);
  } catch (error) {
    setAttachmentGroupError(
      source,
      scopeId,
      safeErrorMessage(error),
    );
  } finally {
    updateAttachmentGroup(source, scopeId, (current) => ({
      ...current,
      uploading: false,
    }));
  }
}

function attachmentInput(source) {
  return source === "new"
    ? elements.newAttachmentInput
    : elements.caseAttachmentInput;
}

function attachmentTrigger(source) {
  return source === "new"
    ? elements.newAttachmentTrigger
    : elements.caseAttachmentTrigger;
}

function updateAttachmentGroup(source, scopeId, update) {
  store.setState((state) => {
    const group = state.attachments[source];
    if (group.scopeId !== scopeId) {
      return state;
    }
    const next = update(group, state);
    if (!next || next === group) {
      return state;
    }
    return {
      ...state,
      attachments: {
        ...state.attachments,
        [source]: next,
      },
    };
  });
}

function setAttachmentGroupError(source, scopeId, message) {
  updateAttachmentGroup(source, scopeId, (group) => ({
    ...group,
    error: message,
  }));
}

function groupWithItems(group, items, patch = {}) {
  return {
    ...group,
    ...patch,
    items,
    draftIds: attachmentDraftIds(items),
  };
}

function mergeRestoredAttachmentItems(restored, current) {
  const currentById = new Map(
    current
      .filter((item) => typeof item.id === "string")
      .map((item) => [item.id, item]),
  );
  const merged = [];
  const keys = new Set();
  for (const restoredItem of restored) {
    const item = currentById.get(restoredItem.id) || restoredItem;
    appendUniqueAttachmentItem(merged, keys, item);
  }
  for (const item of current) {
    appendUniqueAttachmentItem(merged, keys, item);
  }
  return merged;
}

function appendUniqueAttachmentItem(items, keys, item) {
  const key = item.id || item.ui_key || "";
  if (!key || keys.has(key)) {
    return;
  }
  keys.add(key);
  items.push(item);
}

function replaceGroupItem(group, key, replacement) {
  const index = group.items.findIndex(
    (item) => attachmentItemKey(item) === key,
  );
  if (index < 0) {
    return group;
  }
  const items = [...group.items];
  items[index] = replacement;
  return groupWithItems(group, items);
}

function persistAttachmentGroup(source, scopeId) {
  const group = store.getState().attachments[source];
  if (group.scopeId !== scopeId) {
    return;
  }
  const ids = attachmentDraftIds(group.items);
  rememberAttachmentDraftIds(scopeId, ids);
  if (!sameValues(group.draftIds, ids)) {
    updateAttachmentGroup(source, scopeId, (current) => ({
      ...current,
      draftIds: ids,
    }));
  }
}

function attachmentDraftIds(items) {
  return [
    ...new Set(
      items
        .map((item) => item.id)
        .filter((id) => typeof id === "string"),
    ),
  ].slice(0, MAX_ATTACHMENT_COUNT);
}

function attachmentGroupOccupancy(group) {
  const ids = new Set(group.draftIds);
  let placeholders = 0;
  for (const item of group.items) {
    if (typeof item.id === "string") {
      ids.add(item.id);
    } else {
      placeholders += 1;
    }
  }
  return ids.size + placeholders;
}

function sameReviewIdentity(candidate, expected) {
  return Boolean(
    candidate &&
    expected &&
    candidate.source === expected.source &&
    candidate.scopeId === expected.scopeId &&
    candidate.key === expected.key &&
    candidate.item?.id === expected.item?.id
  );
}

function consultationAttachmentIds(group) {
  if (!group.restored || group.restoring) {
    return {
      ids: [],
      error: "请等待本轮材料恢复完成后再发送。",
    };
  }
  if (group.uploading || group.working) {
    return {
      ids: [],
      error: "请等待本轮材料处理完成后再发送。",
    };
  }
  if (group.items.length > MAX_ATTACHMENT_COUNT) {
    return {
      ids: [],
      error: `本轮最多添加 ${MAX_ATTACHMENT_COUNT} 个材料。`,
    };
  }
  if (
    group.items.some(
      (item) => item.status !== "confirmed" || !item.id,
    )
  ) {
    return {
      ids: [],
      error: "请先核对、重试或移除尚未确认的材料。",
    };
  }
  const ids = attachmentDraftIds(group.items);
  if (ids.length !== group.items.length) {
    return {
      ids: [],
      error: "材料状态已变化，请刷新后重新确认。",
    };
  }
  const contextLength = Array.from(
    group.items
      .map((item) => item.confirmed_text || "")
      .join(""),
  ).length;
  if (contextLength > MAX_ATTACHMENT_CONTEXT_LENGTH) {
    return {
      ids: [],
      error:
        `本轮材料确认文字不能超过 ${MAX_ATTACHMENT_CONTEXT_LENGTH} 个字符。`,
    };
  }
  return { ids, error: "" };
}

function rememberDetachedAttachment(scopeId, item) {
  if (!item?.id || item.status === "bound") {
    return;
  }
  rememberAttachmentDraftIds(
    scopeId,
    [...readAttachmentDraftIds(scopeId), item.id],
  );
}

function isCurrentAttachmentScope(source, scopeId) {
  return store.getState().attachments[source].scopeId === scopeId;
}

function attachmentItemKey(item) {
  return item.ui_key || item.id || "";
}

function attachmentForDisplay(item, fileAvailable = false) {
  if (item.status !== "failed") {
    return item;
  }
  const canRetry =
    fileAvailable &&
    [
      "attachment_extraction_timeout",
      "attachment_service_unavailable",
    ].includes(item.error_code);
  return {
    ...item,
    can_retry: canRetry,
    ui_error: attachmentFailureMessage(item.error_code),
  };
}

function validateSelectedFile(file) {
  if (
    file === null ||
    typeof file !== "object" ||
    typeof file.name !== "string"
  ) {
    return "请选择需要添加的材料。";
  }
  if (
    !file.name.trim() ||
    Array.from(file.name).length > 255 ||
    /[\u0000-\u001f\u007f-\u009f]/u.test(file.name)
  ) {
    return "文件名无效，请修改文件名后重新选择。";
  }
  if (!/\.(pdf|png|jpe?g)$/iu.test(file.name)) {
    return "仅支持 PDF、PNG、JPG 或 JPEG 文件。";
  }
  if (!Number.isInteger(file.size) || file.size > MAX_FILE_BYTES) {
    return "单个材料不能超过 10 MiB。";
  }
  return null;
}

function readReviewText() {
  return [
    ...elements.attachmentReviewPages.querySelectorAll(
      "[data-review-block]",
    ),
  ]
    .map((textarea) => textarea.value.trim())
    .filter(Boolean)
    .join("\n\n");
}

function reviewTotalLength(review, confirmedText) {
  const group = store.getState().attachments[review.source];
  const otherText = group.items
    .filter(
      (item) =>
        attachmentItemKey(item) !== review.key &&
        item.status === "confirmed",
    )
    .map((item) => item.confirmed_text || "")
    .join("");
  return (
    Array.from(otherText).length +
    Array.from(confirmedText).length
  );
}

function setReviewError(message) {
  store.setState((state) => ({
    ...state,
    attachments: {
      ...state.attachments,
      review: state.attachments.review
        ? {
          ...state.attachments.review,
          saving: false,
          error: message,
        }
        : null,
    },
  }));
}

function isDiscardedAttachmentError(error) {
  return (
    error instanceof ApiError &&
    [
      "attachment_not_found",
      "attachment_already_bound",
    ].includes(error.code)
  );
}

function attachmentFailureMessage(code) {
  const messages = {
    attachment_type_unsupported:
      "文件类型不受支持，请重新选择 PDF 或图片。",
    attachment_type_mismatch:
      "文件内容与扩展名不一致，请检查后重新选择。",
    attachment_name_invalid:
      "文件名无效，请修改文件名后重新选择。",
    attachment_pdf_encrypted:
      "PDF 已加密，请解除密码后重新选择。",
    attachment_corrupt:
      "文件无法读取，可能已经损坏。",
    attachment_text_empty:
      "没有提取到可核对的文字，请换一份清晰材料。",
    attachment_too_large:
      "文件超过大小限制，请压缩后重新选择。",
    attachment_page_limit_exceeded:
      "PDF 页数超过限制，请拆分后重新选择。",
    attachment_pixel_limit_exceeded:
      "图片尺寸过大，请缩小后重新选择。",
    attachment_extracted_text_too_long:
      "材料文字过长，请拆分并只保留相关部分。",
    attachment_extraction_timeout:
      "文字提取超时，可以重试。",
    attachment_service_unavailable:
      "本地文字提取暂时不可用，可以稍后重试。",
  };
  return messages[code] || "材料处理没有完成，请重新选择或移除。";
}

function sameValues(left, right) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

async function refreshHistory({ restoreId = null } = {}) {
  if (!hasWorkspaceAccess(store.getState().identity)) {
    return;
  }
  const generation = ++historyGeneration;
  store.setState((state) => ({
    ...state,
    history: {
      ...state.history,
      status:
        state.history.items.length === 0
          ? "loading"
          : state.history.status,
      refreshing: true,
      error: null,
    },
  }));

  try {
    const payload = await listSessions();
    if (generation !== historyGeneration) {
      return;
    }
    store.setState((state) => {
      const currentSummary = payload.sessions.find(
        (item) => item.session_id === state.currentSessionId,
      );
      return {
        ...state,
        history: {
          status: "ready",
          items: payload.sessions,
          refreshing: false,
          error: null,
        },
        session: currentSummary || state.session,
      };
    });

    if (
      restoreId &&
      store.getState().currentSessionId === restoreId
    ) {
      const exists = payload.sessions.some(
        (item) => item.session_id === restoreId,
      );
      if (exists) {
        await openSession(restoreId);
      } else {
        rememberCurrentSessionId(null);
        store.setState((state) => ({
          ...state,
          currentSessionId: null,
          session: null,
        }));
      }
    }
  } catch (error) {
    if (generation !== historyGeneration) {
      return;
    }
    store.setState((state) => ({
      ...state,
      history: {
        ...state.history,
        status: "error",
        refreshing: false,
        error: safeErrorMessage(error),
      },
    }));
  }
}

async function openSession(sessionId) {
  if (
    !hasWorkspaceAccess(store.getState().identity) ||
    store.getState().attachments.review?.saving
  ) {
    return;
  }
  if (elements.attachmentReviewDialog.open) {
    closeReview();
  }
  const generation = ++detailGeneration;
  consultGeneration += 1;
  attachmentRestoreGenerations.case += 1;
  const summary = store
    .getState()
    .history.items.find((item) => item.session_id === sessionId);
  const caseAttachments = createAttachmentGroup(sessionId);

  store.setState((state) => ({
    ...state,
    view: "case",
    historyOpen: false,
    currentSessionId: sessionId,
    session: summary || null,
    turns:
      state.currentSessionId === sessionId ? state.turns : [],
    draftCase: "",
    busy: {
      ...state.busy,
      consult: false,
      session: true,
    },
    inputErrors: { ...state.inputErrors, case: "" },
    caseError: null,
    sessionExpired: false,
    attachments: {
      ...state.attachments,
      case: caseAttachments,
      review: null,
    },
  }));

  try {
    const detail = await getSession(sessionId);
    if (generation !== detailGeneration) {
      return;
    }
    rememberCurrentSessionId(sessionId);
    store.setState((state) => ({
      ...state,
      view: "case",
      currentSessionId: sessionId,
      session: detail.session,
      turns: detail.turns,
      caseError: null,
      sessionExpired: false,
    }));
    await restoreAttachmentGroup("case", sessionId);
    if (generation !== detailGeneration) {
      return;
    }
    store.setState((state) => ({
      ...state,
      busy: { ...state.busy, session: false },
    }));
    scrollThreadToEnd();
  } catch (error) {
    if (generation !== detailGeneration) {
      return;
    }
    if (error instanceof ApiError && error.code === "session_not_found") {
      rememberCurrentSessionId(null);
      store.setState((state) => ({
        ...state,
        view: "new",
        currentSessionId: null,
        session: null,
        turns: [],
        attachments: {
          ...state.attachments,
          case: groupWithItems(state.attachments.case, [], {
            restored: true,
            restoring: false,
            uploading: false,
            working: false,
          }),
          review: null,
        },
        busy: { ...state.busy, session: false },
        history: {
          ...state.history,
          items: state.history.items.filter(
            (item) => item.session_id !== sessionId,
          ),
        },
      }));
      showToast("这条咨询不存在或已经过期。", "error");
      return;
    }
    store.setState((state) => ({
      ...state,
      busy: { ...state.busy, session: false },
      caseError: {
        message: safeErrorMessage(error),
        retryable: true,
        action: "load",
        sessionId,
      },
    }));
  }
}

async function submitConsultation(source) {
  let state = store.getState();
  if (
    state.busy.consult ||
    state.busy.session ||
    identityActionPending
  ) {
    return;
  }
  const initiallyHasWorkspace = hasWorkspaceAccess(state.identity);
  const field = source === "new" ? "draftNew" : "draftCase";
  const draft = state[field];
  const message = draft.trim();
  const length = Array.from(message).length;
  const maximum = initiallyHasWorkspace
    ? MAX_REGISTERED_MESSAGE_LENGTH
    : MAX_TRIAL_MESSAGE_LENGTH;

  if (!message) {
    setInputError(source, "请先写下需要咨询的情况。");
    focusComposer(source);
    return;
  }
  if (length > maximum) {
    setInputError(
      source,
      `内容不能超过 ${maximum} 个字符。`,
    );
    focusComposer(source);
    return;
  }
  if (!consultationIdentityAllowed(state.identity)) {
    setInputError(source, "当前账号状态暂时无法开始咨询。");
    return;
  }
  if (consultationQuotaExhausted(state.identity)) {
    setInputError(source, "当前咨询额度已经用完。");
    return;
  }
  if (source === "case" && !state.currentSessionId) {
    store.setState((current) => ({
      ...current,
      sessionExpired: true,
      caseError: {
        message: "这条咨询已经无法继续，请开始新咨询。",
        expired: true,
        action: "new",
        tone: "warning",
      },
    }));
    return;
  }

  identityActionPending = true;
  try {
    await prepareConsultationIdentity(source);
  } catch (error) {
    if (!(error instanceof PrivacyCancelledError)) {
      setInputError(source, safeErrorMessage(error));
      focusComposer(source);
    }
    return;
  } finally {
    identityActionPending = false;
  }

  state = store.getState();
  const workspaceAccess = hasWorkspaceAccess(state.identity);
  const registeredAccount = hasRegisteredAccount(state.identity);
  const attachmentGroup = state.attachments[source];
  const attachmentCheck = workspaceAccess
    ? consultationAttachmentIds(attachmentGroup)
    : { ids: [], error: "" };
  if (attachmentCheck.error) {
    setAttachmentGroupError(
      source,
      attachmentGroup.scopeId,
      attachmentCheck.error,
    );
    attachmentTrigger(source).focus();
    return;
  }
  const attachmentIds = attachmentCheck.ids;
  const attachmentScopeId = attachmentGroup.scopeId;
  const sessionId =
    source === "case" ? state.currentSessionId : null;
  const generation = ++consultGeneration;
  store.setState((current) => ({
    ...current,
    busy: { ...current.busy, consult: true },
    inputErrors: { ...current.inputErrors, [source]: "" },
    caseError: source === "case" ? null : current.caseError,
    attachments: {
      ...current.attachments,
      [source]: {
        ...current.attachments[source],
        error: "",
      },
    },
  }));

  try {
    const response = registeredAccount
      ? await registeredConsultation(
        {
          message,
          sessionId,
          attachmentIds,
        },
        source === "new" ? elements.newSend : elements.caseSend,
      )
      : (
        workspaceAccess
          ? await consult({ message, sessionId, attachmentIds })
          : await trialConsult({ message, sessionId })
      );
    if (generation !== consultGeneration) {
      return;
    }
    const createdAt = new Date().toISOString();
    const boundIds = new Set(
      response.attachments.map((attachment) => attachment.id),
    );
    const unboundAttachmentIds =
      response.turn_kind === "new_case"
        ? attachmentIds
        : [];
    const turn = {
      turn_id: response.turn_id,
      user_message: message,
      response,
      created_at: createdAt,
      ...(unboundAttachmentIds.length > 0
        ? {
          unbound_attachment_ids: unboundAttachmentIds,
        }
        : {}),
    };
    if (!workspaceAccess) {
      store.setState((current) => ({
        ...current,
        view: "case",
        currentSessionId: response.session_id,
        session: provisionalSession(
          current.session,
          response,
          source === "new"
            ? message
            : current.turns[0]?.user_message || message,
          createdAt,
        ),
        turns:
          source === "new" ? [turn] : [...current.turns, turn],
        draftNew: source === "new" ? "" : current.draftNew,
        draftCase: "",
        busy: { ...current.busy, consult: false },
        inputErrors: { new: "", case: "" },
        caseError: null,
        sessionExpired: false,
        identity: {
          ...current.identity,
          quota: response.quota || current.identity.quota,
        },
      }));
      scrollThreadToEnd();
      if (response.status === "need_more_facts") {
        requestAnimationFrame(() => elements.caseMessage.focus());
      }
      return;
    }

    for (const item of attachmentGroup.items) {
      if (boundIds.has(item.id)) {
        retryFiles.delete(attachmentItemKey(item));
        retryFiles.delete(item.id);
      }
    }
    rememberCurrentSessionId(response.session_id);
    store.setState((current) => {
      const turns =
        source === "new" ? [turn] : [...current.turns, turn];
      const currentGroup = current.attachments[source];
      const remainingItems = currentGroup.items.filter(
        (item) => !boundIds.has(item.id),
      );
      const nextGroup = groupWithItems(
        currentGroup,
        remainingItems,
        {
          uploading: false,
          working: false,
          error: "",
        },
      );
      const attachments = {
        ...current.attachments,
        [source]: nextGroup,
        review: null,
      };
      if (source === "new") {
        const caseGroup = createAttachmentGroup(
          response.session_id,
        );
        caseGroup.restored = caseGroup.draftIds.length === 0;
        attachments.case = caseGroup;
      }
      return {
        ...current,
        view: "case",
        currentSessionId: response.session_id,
        session: provisionalSession(
          current.session,
          response,
          turns[0].user_message,
          createdAt,
        ),
        turns,
        draftNew: source === "new" ? "" : current.draftNew,
        draftCase: "",
        busy: { ...current.busy, consult: false },
        inputErrors: { new: "", case: "" },
        caseError: null,
        sessionExpired: false,
        attachments,
        identity: {
          ...current.identity,
          quota: response.quota || current.identity.quota,
        },
      };
    });
    persistAttachmentGroup(source, attachmentScopeId);
    scrollThreadToEnd();
    if (response.status === "need_more_facts") {
      requestAnimationFrame(() => elements.caseMessage.focus());
    }
    void refreshHistory();
  } catch (error) {
    if (generation !== consultGeneration) {
      return;
    }
    if (error instanceof PrivacyCancelledError) {
      return;
    }
    if (source === "case") {
      handleCaseConsultError(error);
    } else {
      store.setState((current) => ({
        ...current,
        busy: { ...current.busy, consult: false },
        inputErrors: {
          ...current.inputErrors,
          new: safeErrorMessage(error),
        },
      }));
      focusComposer("new");
    }
  } finally {
    if (
      generation === consultGeneration &&
      store.getState().busy.consult
    ) {
      store.setState((current) => ({
        ...current,
        busy: { ...current.busy, consult: false },
      }));
    }
  }
}

async function prepareConsultationIdentity(source) {
  const state = store.getState();
  const trigger =
    source === "new" ? elements.newSend : elements.caseSend;
  if (state.identity.status === "local") {
    return;
  }
  if (hasRegisteredAccount(state.identity)) {
    if (state.identity.privacyAcceptanceRequired) {
      await requestRegisteredPrivacyAcceptance(trigger);
    }
    return;
  }
  if (state.identity.trialIdentityId) {
    return;
  }
  if (source !== "new") {
    throw new Error("匿名试用身份已失效，请开始新咨询。");
  }
  const started = await privacy.requestAcceptance({
    purpose: "trial",
    trigger,
    onAccept: async (policyDocument) => {
      const captchaToken = anonymousCaptcha.consumeToken();
      return startTrial({
        captchaToken,
        privacyVersion: policyDocument.version,
        privacyAccepted: true,
      });
    },
  });
  store.setState((current) => ({
    ...current,
    identity: {
      ...current.identity,
      trialIdentityId: started.identity_id,
      quota: started.quota,
      privacyVersion: privacy.getPolicy()?.version || "",
      privacyAcceptanceRequired: false,
    },
  }));
}

async function registeredConsultation(payload, trigger) {
  try {
    return await consult(payload);
  } catch (error) {
    if (
      !(error instanceof ApiError) ||
      error.code !== "privacy_acceptance_required"
    ) {
      throw error;
    }
    store.setState((state) => ({
      ...state,
      identity: {
        ...state.identity,
        privacyAcceptanceRequired: true,
      },
    }));
    await requestRegisteredPrivacyAcceptance(trigger);
    return consult(payload);
  }
}

async function requestRegisteredPrivacyAcceptance(trigger) {
  await privacy.requestAcceptance({
    purpose: "consultation",
    trigger,
    onAccept: async (policyDocument) => {
      const accepted = await acceptPrivacy(policyDocument.version);
      store.setState((state) => ({
        ...state,
        identity: {
          ...state.identity,
          privacyVersion: accepted.version,
          privacyAcceptanceRequired: false,
        },
      }));
      return accepted;
    },
  });
}

function handleCaseConsultError(error) {
  if (error instanceof ApiError && error.code === "session_not_found") {
    if (hasWorkspaceAccess(store.getState().identity)) {
      rememberCurrentSessionId(null);
    }
    store.setState((state) => ({
      ...state,
      busy: { ...state.busy, consult: false },
      sessionExpired: true,
      caseError: {
        message: "这条咨询不存在或已经过期，当前内容已转为只读。",
        expired: true,
        action: "new",
        tone: "warning",
      },
    }));
    return;
  }
  if (
    error instanceof ApiError &&
    error.code === "case_no_progress"
  ) {
    store.setState((state) => ({
      ...state,
      busy: { ...state.busy, consult: false },
      caseError: {
        message: safeErrorMessage(error),
        retryable: false,
        tone: "warning",
      },
    }));
    focusComposer("case");
    return;
  }
  if (
    error instanceof ApiError &&
    error.code === "consultation_conflict"
  ) {
    store.setState((state) => ({
      ...state,
      busy: { ...state.busy, consult: false },
      caseError: {
        message: safeErrorMessage(error),
        retryable: true,
        action: "consult",
        retryLabel: "重新提交",
        tone: "warning",
      },
    }));
    focusComposer("case");
    return;
  }
  store.setState((state) => ({
    ...state,
    busy: { ...state.busy, consult: false },
    caseError: {
      message: safeErrorMessage(error),
      retryable: error instanceof ApiError ? error.retryable : true,
      action: "consult",
    },
  }));
}

function retryCase() {
  const state = store.getState();
  const error = state.caseError;
  if (!error) {
    return;
  }
  if (
    error.action === "load" &&
    error.sessionId &&
    hasWorkspaceAccess(state.identity)
  ) {
    void openSession(error.sessionId);
  } else if (error.action === "consult") {
    void submitConsultation("case");
  }
}

function startNew() {
  if (store.getState().attachments.review?.saving) {
    return;
  }
  resetToNewConsultation("");
}

function continueAsNew(
  message,
  attachmentIds = [],
  turnId = null,
) {
  const state = store.getState();
  if (
    !hasWorkspaceAccess(state.identity) ||
    state.attachments.review?.saving
  ) {
    return;
  }
  let ids = [
    ...new Set(
      attachmentIds.filter((id) => typeof id === "string"),
    ),
  ];
  if (ids.length === 0) {
    const recovered = historicalNewCaseAttachmentIds(state, turnId);
    if (recovered.error) {
      showToast(recovered.error, "error");
      return;
    }
    ids = recovered.ids;
  }
  if (ids.length === 0) {
    resetToNewConsultation(message);
    return;
  }
  if (
    !state.attachments.case.restored ||
    state.attachments.case.restoring
  ) {
    showToast(
      "请等待当前案件的材料恢复完成后再继续分案。",
      "error",
    );
    return;
  }

  const existingIds = new Set(
    [
      ...state.attachments.new.draftIds,
      ...state.attachments.new.items
        .map((item) => item.id)
        .filter(Boolean),
    ],
  );
  const movingItems = [];
  for (const attachmentId of ids) {
    if (existingIds.has(attachmentId)) {
      continue;
    }
    const item = state.attachments.case.items.find(
      (candidate) =>
        candidate.id === attachmentId &&
        candidate.status === "confirmed",
    );
    if (!item) {
      showToast(
        "这些材料的状态已经变化，请在当前案件中重新确认。",
        "error",
      );
      return;
    }
    movingItems.push(item);
  }
  if (
    attachmentGroupOccupancy(state.attachments.new) +
      movingItems.length >
    MAX_ATTACHMENT_COUNT
  ) {
    showToast(
      "新咨询已有材料，请先移除部分材料后再继续分案。",
      "error",
    );
    return;
  }

  const nextNewItems = [
    ...state.attachments.new.items,
    ...movingItems,
  ];
  const nextNew = {
    ...groupWithItems(
      state.attachments.new,
      nextNewItems,
      { error: "" },
    ),
    draftIds: [
      ...new Set([
        ...state.attachments.new.draftIds,
        ...attachmentDraftIds(nextNewItems),
      ]),
    ].slice(0, MAX_ATTACHMENT_COUNT),
  };
  const nextCase = groupWithItems(
    state.attachments.case,
    state.attachments.case.items.filter(
      (item) => !ids.includes(item.id),
    ),
    {
      error: "",
      restoredDraftIds:
        state.attachments.case.restoredDraftIds.filter(
          (id) => !ids.includes(id),
        ),
    },
  );
  resetToNewConsultation(message, {
    new: nextNew,
    case: nextCase,
  });
  rememberAttachmentDraftIds(null, nextNew.draftIds);
  rememberAttachmentDraftIds(nextCase.scopeId, nextCase.draftIds);
}

function historicalNewCaseAttachmentIds(state, turnId) {
  const latestTurn = state.turns[state.turns.length - 1];
  if (
    typeof turnId !== "string" ||
    latestTurn?.turn_id !== turnId ||
    latestTurn.response?.turn_kind !== "new_case"
  ) {
    return { ids: [], error: "" };
  }
  const group = state.attachments.case;
  if (!group.restored || group.restoring) {
    return {
      ids: [],
      error: "请等待当前案件的材料恢复完成后再继续分案。",
    };
  }
  const itemsById = new Map(
    group.items
      .filter((item) => typeof item.id === "string")
      .map((item) => [item.id, item]),
  );
  const ids = [];
  for (const attachmentId of group.restoredDraftIds) {
    if (itemsById.get(attachmentId)?.status !== "confirmed") {
      return {
        ids: [],
        error: "这些材料的状态已经变化，请在当前案件中重新确认。",
      };
    }
    ids.push(attachmentId);
  }
  return { ids, error: "" };
}

function resetToNewConsultation(prefill, attachmentGroups = null) {
  const draft = String(prefill || "").trim();
  consultGeneration += 1;
  detailGeneration += 1;
  attachmentRestoreGenerations.case += 1;
  if (hasWorkspaceAccess(store.getState().identity)) {
    rememberCurrentSessionId(null);
  }
  store.setState((state) => ({
    ...state,
    view: "new",
    historyOpen: false,
    currentSessionId: null,
    session: null,
    turns: [],
    draftNew: draft,
    draftCase: "",
    busy: {
      ...state.busy,
      consult: false,
      session: false,
    },
    inputErrors: { new: "", case: "" },
    caseError: null,
    sessionExpired: false,
    attachments: {
      ...state.attachments,
      ...(attachmentGroups || {}),
      review: null,
    },
  }));
  if (elements.attachmentReviewDialog.open) {
    elements.attachmentReviewDialog.close();
  }
  requestAnimationFrame(() => {
    elements.newMessage.focus();
    elements.newMessage.setSelectionRange(
      draft.length,
      draft.length,
    );
  });
}

function requestDelete(session) {
  if (
    !hasWorkspaceAccess(store.getState().identity) ||
    store.getState().busy.delete
  ) {
    return;
  }
  store.setState((state) => ({
    ...state,
    deleteTarget: session,
  }));
  elements.deleteDescription.textContent =
    `“${session.title}”及相关对话和案件摘要将从本机移除。`;
  if (!elements.deleteDialog.open) {
    elements.deleteDialog.showModal();
  }
}

async function confirmDelete() {
  const target = store.getState().deleteTarget;
  if (
    !hasWorkspaceAccess(store.getState().identity) ||
    !target ||
    store.getState().busy.delete
  ) {
    return;
  }
  const originalLabel = elements.deleteConfirm.textContent;
  elements.deleteConfirm.disabled = true;
  elements.deleteConfirm.textContent = "删除中";
  store.setState((state) => ({
    ...state,
    busy: { ...state.busy, delete: true },
  }));

  try {
    await deleteSession(target.session_id);
    await deleteAttachmentDrafts(target.session_id);
    const deletingCurrent =
      store.getState().currentSessionId === target.session_id;
    if (deletingCurrent) {
      rememberCurrentSessionId(null);
      detailGeneration += 1;
      consultGeneration += 1;
      attachmentRestoreGenerations.case += 1;
      for (const item of store.getState().attachments.case.items) {
        retryFiles.delete(attachmentItemKey(item));
        if (item.id) {
          retryFiles.delete(item.id);
        }
      }
    }
    store.setState((state) => ({
      ...state,
      view: deletingCurrent ? "new" : state.view,
      currentSessionId: deletingCurrent
        ? null
        : state.currentSessionId,
      session: deletingCurrent ? null : state.session,
      turns: deletingCurrent ? [] : state.turns,
      draftCase: deletingCurrent ? "" : state.draftCase,
      sessionExpired: deletingCurrent ? false : state.sessionExpired,
      caseError: deletingCurrent ? null : state.caseError,
      attachments: deletingCurrent
        ? {
          ...state.attachments,
          case: groupWithItems(state.attachments.case, [], {
            restored: true,
            restoring: false,
            uploading: false,
            working: false,
            error: "",
          }),
          review: null,
        }
        : state.attachments,
      history: {
        ...state.history,
        items: state.history.items.filter(
          (item) => item.session_id !== target.session_id,
        ),
      },
      busy: { ...state.busy, delete: false },
      deleteTarget: null,
    }));
    elements.deleteDialog.close();
    showToast("咨询记录已删除。", "info");
  } catch (error) {
    store.setState((state) => ({
      ...state,
      busy: { ...state.busy, delete: false },
    }));
    showToast(safeErrorMessage(error), "error");
  } finally {
    elements.deleteConfirm.disabled = false;
    elements.deleteConfirm.textContent = originalLabel;
  }
}

async function deleteAttachmentDrafts(sessionId) {
  if (!hasWorkspaceAccess(store.getState().identity)) {
    return;
  }
  const attachmentIds = readAttachmentDraftIds(sessionId);
  await Promise.allSettled(
    attachmentIds.map((attachmentId) =>
      deleteAttachment(attachmentId),
    ),
  );
  rememberAttachmentDraftIds(sessionId, []);
}

function updateDraft(source, value) {
  const field = source === "new" ? "draftNew" : "draftCase";
  store.setState((state) => ({
    ...state,
    [field]: value,
    inputErrors: {
      ...state.inputErrors,
      [source]: "",
    },
  }));
}

function setInputError(source, message) {
  store.setState((state) => ({
    ...state,
    inputErrors: {
      ...state.inputErrors,
      [source]: message,
    },
  }));
}

function setHistoryOpen(open) {
  if (
    open &&
    !hasWorkspaceAccess(store.getState().identity)
  ) {
    return;
  }
  store.setState((state) => ({
    ...state,
    historyOpen: open,
  }));
}

function bindEnterToSubmit(textarea, form) {
  textarea.addEventListener("keydown", (event) => {
    if (
      event.key !== "Enter" ||
      event.shiftKey ||
      event.isComposing ||
      event.keyCode === 229
    ) {
      return;
    }
    event.preventDefault();
    form.requestSubmit();
  });
}

function provisionalSession(
  existing,
  response,
  firstMessage,
  createdAt,
) {
  const now = new Date();
  const expiresAt = new Date(
    now.getTime() + 72 * 60 * 60 * 1000,
  ).toISOString();
  return {
    session_id: response.session_id,
    title: existing?.title || historyTitle(firstMessage),
    scenario_id: existing?.scenario_id || null,
    status: response.status,
    created_at: existing?.created_at || createdAt,
    updated_at: createdAt,
    expires_at: existing?.expires_at || expiresAt,
  };
}

function historyTitle(message) {
  const normalized = message.replace(/\s+/g, " ").trim();
  const characters = Array.from(normalized);
  return characters.length <= 24
    ? normalized
    : `${characters.slice(0, 24).join("")}…`;
}

function safeErrorMessage(error) {
  if (error instanceof ApiError) {
    return error.userMessage;
  }
  return "操作没有完成，请稍后重试。";
}

function consultationIdentityAllowed(identity) {
  return [
    "local",
    "trial",
    "pending_verification",
    "capacity_full",
    "authenticated",
  ].includes(identity.status);
}

function consultationQuotaExhausted(identity) {
  if (!identity.quota) {
    return false;
  }
  if (hasRegisteredAccount(identity)) {
    return (
      identity.quota.remaining_daily <= 0 ||
      identity.quota.remaining_monthly <= 0
    );
  }
  return identity.quota.remaining_total <= 0;
}

function focusComposer(source) {
  requestAnimationFrame(() => {
    if (source === "new") {
      elements.newMessage.focus();
    } else {
      elements.caseMessage.focus();
    }
  });
}

function scrollThreadToEnd() {
  requestAnimationFrame(() => {
    elements.thread.lastElementChild?.scrollIntoView({
      block: "end",
    });
  });
}

function showToast(message, tone) {
  const id = ++toastGeneration;
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
  }
  store.setState((state) => ({
    ...state,
    toast: { id, message, tone },
  }));
  toastTimer = window.setTimeout(() => {
    if (store.getState().toast?.id === id) {
      store.setState((state) => ({ ...state, toast: null }));
    }
  }, 4200);
}

function requiredElement(id) {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element: ${id}`);
  }
  return element;
}

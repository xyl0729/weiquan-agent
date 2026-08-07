import {
  ApiError,
  consult,
  deleteSession,
  getHealth,
  getSession,
  listSessions,
} from "./api.js";
import { createRenderer } from "./render.js";
import {
  createStore,
  rememberCurrentSessionId,
} from "./state.js";

const MAX_MESSAGE_LENGTH = 4000;
const store = createStore();
let consultGeneration = 0;
let detailGeneration = 0;
let historyGeneration = 0;
let toastGeneration = 0;
let toastTimer = null;

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
  thread: requiredElement("thread"),
  deleteDialog: requiredElement("delete-dialog"),
  deleteDescription: requiredElement("delete-dialog-description"),
  deleteConfirm: requiredElement("delete-confirm"),
};

const render = createRenderer({
  continueAsNew,
  openSession,
  requestDelete,
  retryCase,
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
  const rememberedSessionId = store.getState().currentSessionId;
  await Promise.allSettled([
    loadHealth(),
    refreshHistory({ restoreId: rememberedSessionId }),
  ]);
}

async function loadHealth() {
  store.setState((state) => ({
    ...state,
    service: { status: "loading", label: "正在连接" },
  }));
  try {
    const health = await getHealth();
    const available = health.status === "ok";
    store.setState((state) => ({
      ...state,
      service: {
        status: available ? "ok" : "error",
        label: available ? "本地服务可用" : "部分服务不可用",
      },
    }));
  } catch {
    store.setState((state) => ({
      ...state,
      service: { status: "error", label: "连接失败" },
    }));
  }
}

async function refreshHistory({ restoreId = null } = {}) {
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
  const generation = ++detailGeneration;
  consultGeneration += 1;
  const summary = store
    .getState()
    .history.items.find((item) => item.session_id === sessionId);

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
      busy: { ...state.busy, session: false },
      caseError: null,
      sessionExpired: false,
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
  const state = store.getState();
  if (state.busy.consult || state.busy.session) {
    return;
  }
  const field = source === "new" ? "draftNew" : "draftCase";
  const draft = state[field];
  const message = draft.trim();
  const length = Array.from(message).length;

  if (!message) {
    setInputError(source, "请先写下需要咨询的情况。");
    focusComposer(source);
    return;
  }
  if (length > MAX_MESSAGE_LENGTH) {
    setInputError(
      source,
      `内容不能超过 ${MAX_MESSAGE_LENGTH} 个字符。`,
    );
    focusComposer(source);
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

  const sessionId =
    source === "case" ? state.currentSessionId : null;
  const generation = ++consultGeneration;
  store.setState((current) => ({
    ...current,
    busy: { ...current.busy, consult: true },
    inputErrors: { ...current.inputErrors, [source]: "" },
    caseError: source === "case" ? null : current.caseError,
  }));

  try {
    const response = await consult({ message, sessionId });
    if (generation !== consultGeneration) {
      return;
    }
    const createdAt = new Date().toISOString();
    const turn = {
      turn_id: response.turn_id,
      user_message: message,
      response,
      created_at: createdAt,
    };
    rememberCurrentSessionId(response.session_id);
    store.setState((current) => {
      const turns =
        source === "new" ? [turn] : [...current.turns, turn];
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
      };
    });
    scrollThreadToEnd();
    if (response.status === "need_more_facts") {
      requestAnimationFrame(() => elements.caseMessage.focus());
    }
    void refreshHistory();
  } catch (error) {
    if (generation !== consultGeneration) {
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

function handleCaseConsultError(error) {
  if (error instanceof ApiError && error.code === "session_not_found") {
    rememberCurrentSessionId(null);
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
  const error = store.getState().caseError;
  if (!error) {
    return;
  }
  if (error.action === "load" && error.sessionId) {
    void openSession(error.sessionId);
  } else if (error.action === "consult") {
    void submitConsultation("case");
  }
}

function startNew() {
  resetToNewConsultation("");
}

function continueAsNew(message) {
  resetToNewConsultation(message);
}

function resetToNewConsultation(prefill) {
  const draft = String(prefill || "").trim();
  consultGeneration += 1;
  detailGeneration += 1;
  rememberCurrentSessionId(null);
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
  }));
  requestAnimationFrame(() => {
    elements.newMessage.focus();
    elements.newMessage.setSelectionRange(
      draft.length,
      draft.length,
    );
  });
}

function requestDelete(session) {
  if (store.getState().busy.delete) {
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
  if (!target || store.getState().busy.delete) {
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
    const deletingCurrent =
      store.getState().currentSessionId === target.session_id;
    if (deletingCurrent) {
      rememberCurrentSessionId(null);
      detailGeneration += 1;
      consultGeneration += 1;
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

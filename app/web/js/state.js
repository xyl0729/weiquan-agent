const CURRENT_SESSION_KEY = "weiquan.current-session-id";
const ATTACHMENT_DRAFTS_KEY = "weiquan.attachment-drafts";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function createStore() {
  let state = {
    view: "new",
    historyOpen: false,
    service: {
      status: "loading",
      label: "正在连接",
    },
    identity: {
      status: "loading",
      user: null,
      pendingEmail: "",
      trialIdentityId: null,
      quota: null,
      privacyVersion: "",
      privacyAcceptanceRequired: false,
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
      ocr: {
        status: "loading",
        label: "正在检查文字提取",
      },
      new: createAttachmentGroup(null, false),
      case: createAttachmentGroup(null, false),
      review: null,
    },
    busy: {
      consult: false,
      session: false,
      delete: false,
    },
    inputErrors: {
      new: "",
      case: "",
    },
    caseError: null,
    sessionExpired: false,
    deleteTarget: null,
    toast: null,
  };
  const listeners = new Set();

  return {
    getState() {
      return state;
    },
    setState(update) {
      const next =
        typeof update === "function"
          ? update(state)
          : { ...state, ...update };
      if (next === state) {
        return;
      }
      state = next;
      for (const listener of listeners) {
        listener(state);
      }
    },
    subscribe(listener) {
      listeners.add(listener);
      listener(state);
      return () => listeners.delete(listener);
    },
  };
}

export function createAttachmentGroup(
  scopeId,
  restoreDrafts = true,
) {
  const draftIds = restoreDrafts
    ? readAttachmentDraftIds(scopeId)
    : [];
  return {
    scopeId,
    draftIds,
    restoredDraftIds: [],
    items: [],
    restored: !restoreDrafts || draftIds.length === 0,
    restoring: false,
    uploading: false,
    working: false,
    error: "",
  };
}

export function isUuid(value) {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

export function rememberCurrentSessionId(sessionId) {
  try {
    if (sessionId === null) {
      window.sessionStorage.removeItem(CURRENT_SESSION_KEY);
      return;
    }
    if (isUuid(sessionId)) {
      window.sessionStorage.setItem(CURRENT_SESSION_KEY, sessionId);
    }
  } catch {
    // Restricted browsing modes must not block the consultation UI.
  }
}

export function readRememberedSessionId() {
  try {
    const value = window.sessionStorage.getItem(CURRENT_SESSION_KEY);
    if (value !== null && isUuid(value)) {
      return value;
    }
    window.sessionStorage.removeItem(CURRENT_SESSION_KEY);
  } catch {
    return null;
  }
  return null;
}

export function clearAuthenticatedState() {
  try {
    window.sessionStorage.removeItem(CURRENT_SESSION_KEY);
    window.sessionStorage.removeItem(ATTACHMENT_DRAFTS_KEY);
  } catch {
    // The server session is still authoritative if browser storage is blocked.
  }
}

export function readAttachmentDraftIds(sessionId = null) {
  if (sessionId !== null && !isUuid(sessionId)) {
    return [];
  }
  const ledger = readAttachmentLedger();
  if (sessionId === null) {
    return ledger.new;
  }
  return ledger.sessions[sessionId] || [];
}

export function rememberAttachmentDraftIds(sessionId, attachmentIds) {
  if (sessionId !== null && !isUuid(sessionId)) {
    return;
  }
  const ids = cleanUuidList(attachmentIds);
  const ledger = readAttachmentLedger();
  if (sessionId === null) {
    ledger.new = ids;
  } else if (ids.length > 0) {
    ledger.sessions[sessionId] = ids;
  } else {
    delete ledger.sessions[sessionId];
  }
  writeAttachmentLedger(ledger);
}

function readAttachmentLedger() {
  const empty = { new: [], sessions: {} };
  try {
    const raw = window.sessionStorage.getItem(ATTACHMENT_DRAFTS_KEY);
    if (raw === null) {
      return empty;
    }
    const parsed = JSON.parse(raw);
    if (
      parsed === null ||
      typeof parsed !== "object" ||
      Array.isArray(parsed)
    ) {
      return empty;
    }

    const ledger = {
      new: cleanUuidList(parsed.new),
      sessions: {},
    };
    if (
      parsed.sessions !== null &&
      typeof parsed.sessions === "object" &&
      !Array.isArray(parsed.sessions)
    ) {
      for (const [sessionId, ids] of Object.entries(parsed.sessions)) {
        if (isUuid(sessionId)) {
          const cleanIds = cleanUuidList(ids);
          if (cleanIds.length > 0) {
            ledger.sessions[sessionId] = cleanIds;
          }
        }
      }
    }
    return ledger;
  } catch {
    return empty;
  }
}

function writeAttachmentLedger(ledger) {
  try {
    window.sessionStorage.setItem(
      ATTACHMENT_DRAFTS_KEY,
      JSON.stringify({
        new: cleanUuidList(ledger.new),
        sessions: Object.fromEntries(
          Object.entries(ledger.sessions)
            .filter(([sessionId, ids]) => isUuid(sessionId))
            .map(([sessionId, ids]) => [
              sessionId,
              cleanUuidList(ids),
            ])
            .filter(([, ids]) => ids.length > 0),
        ),
      }),
    );
  } catch {
    // Attachment recovery is best effort and must not block text consultation.
  }
}

function cleanUuidList(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return [...new Set(value.filter(isUuid))].slice(0, 3);
}

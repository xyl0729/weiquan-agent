const CURRENT_SESSION_KEY = "weiquan.current-session-id";
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
    history: {
      status: "loading",
      items: [],
      refreshing: false,
      error: null,
    },
    currentSessionId: readRememberedSessionId(),
    session: null,
    turns: [],
    draftNew: "",
    draftCase: "",
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

export function rememberCurrentSessionId(sessionId) {
  try {
    if (sessionId === null) {
      window.sessionStorage.removeItem(CURRENT_SESSION_KEY);
      return;
    }
    if (UUID_PATTERN.test(sessionId)) {
      window.sessionStorage.setItem(CURRENT_SESSION_KEY, sessionId);
    }
  } catch {
    // Private browsing restrictions must not block the consultation UI.
  }
}

function readRememberedSessionId() {
  try {
    const value = window.sessionStorage.getItem(CURRENT_SESSION_KEY);
    if (value !== null && UUID_PATTERN.test(value)) {
      return value;
    }
    window.sessionStorage.removeItem(CURRENT_SESSION_KEY);
  } catch {
    return null;
  }
  return null;
}

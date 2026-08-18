import { ApiError, getPrivacyPolicy } from "./api.js";

export function createPrivacyController({ captcha }) {
  const dialog = requiredElement("privacy-dialog");
  const form = requiredElement("privacy-form");
  const title = requiredElement("privacy-title");
  const description = requiredElement("privacy-description");
  const version = requiredElement("privacy-version");
  const text = requiredElement("privacy-text");
  const acceptance = requiredElement("privacy-acceptance");
  const accept = requiredElement("privacy-accept");
  const captchaSection = requiredElement("trial-captcha-section");
  const error = requiredElement("privacy-error");
  const close = requiredElement("privacy-close");
  const cancel = requiredElement("privacy-cancel");
  const submit = requiredElement("privacy-submit");
  let policy = null;
  let pending = null;
  let returnFocus = null;

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitAcceptance();
  });
  close.addEventListener("click", cancelPending);
  cancel.addEventListener("click", cancelPending);
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    cancelPending();
  });

  return {
    async load() {
      try {
        policy = await getPrivacyPolicy();
        version.textContent = `版本 ${policy.version}`;
        text.textContent = policy.text;
        return policy;
      } catch (loadError) {
        error.textContent = safeErrorMessage(loadError);
        throw loadError;
      }
    },
    getPolicy() {
      return policy;
    },
    async showPolicy(trigger = null) {
      if (!policy) {
        await this.load();
      }
      return open({
        purpose: "read",
        trigger,
        onAccept: null,
      });
    },
    async requestAcceptance({
      purpose,
      trigger = null,
      onAccept,
    }) {
      if (!policy) {
        await this.load();
      }
      return open({ purpose, trigger, onAccept });
    },
    showError(message) {
      error.textContent = message;
    },
  };

  function open({ purpose, trigger, onAccept }) {
    if (pending) {
      return pending.promise;
    }
    const readOnly = purpose === "read";
    const trial = purpose === "trial";
    title.textContent = readOnly
      ? "隐私政策"
      : "确认隐私政策";
    const captchaEnabled = trial && captcha.isEnabled();
    description.textContent = readOnly
      ? "请查看当前生效版本。"
      : (
        trial
          ? (
            captchaEnabled
              ? "首次试用前需要确认当前隐私政策并完成人机验证。"
              : "首次试用前需要确认当前隐私政策。"
          )
          : "继续咨询前需要确认当前生效的隐私政策。"
      );
    acceptance.hidden = readOnly;
    captchaSection.hidden = !captchaEnabled;
    captcha.setVisible(captchaEnabled);
    cancel.textContent = readOnly ? "关闭" : "暂不接受";
    submit.hidden = readOnly;
    accept.checked = false;
    error.textContent = "";
    returnFocus = trigger instanceof HTMLElement
      ? trigger
      : document.activeElement;

    let resolvePromise;
    let rejectPromise;
    const promise = new Promise((resolve, reject) => {
      resolvePromise = resolve;
      rejectPromise = reject;
    });
    pending = {
      purpose,
      onAccept,
      promise,
      resolve: resolvePromise,
      reject: rejectPromise,
    };
    if (!dialog.open) {
      dialog.showModal();
    }
    requestAnimationFrame(() => {
      if (readOnly) {
        cancel.focus();
      } else {
        accept.focus();
      }
    });
    return promise;
  }

  async function submitAcceptance() {
    if (!pending || pending.purpose === "read") {
      return;
    }
    if (!accept.checked) {
      error.textContent = "请先勾选确认当前隐私政策。";
      accept.focus();
      return;
    }
    submit.disabled = true;
    submit.dataset.loading = "true";
    error.textContent = "";
    try {
      const result = pending.onAccept
        ? await pending.onAccept(policy)
        : policy;
      finishPending(result);
    } catch (submitError) {
      error.textContent = safeErrorMessage(submitError);
    } finally {
      submit.disabled = false;
      submit.dataset.loading = "false";
    }
  }

  function cancelPending() {
    if (!pending || submit.disabled) {
      return;
    }
    if (pending.purpose === "read") {
      finishPending(null);
      return;
    }
    const current = pending;
    pending = null;
    dialog.close();
    current.reject(new PrivacyCancelledError());
    restoreFocus();
  }

  function finishPending(result) {
    if (!pending) {
      return;
    }
    const current = pending;
    pending = null;
    dialog.close();
    current.resolve(result);
    restoreFocus();
  }

  function restoreFocus() {
    const target = returnFocus;
    returnFocus = null;
    if (target instanceof HTMLElement && target.isConnected) {
      target.focus();
    }
  }
}

export class PrivacyCancelledError extends Error {
  constructor() {
    super("Privacy acceptance was cancelled");
    this.name = "PrivacyCancelledError";
  }
}

function safeErrorMessage(error) {
  if (error instanceof ApiError) {
    return error.userMessage;
  }
  if (error && typeof error.userMessage === "string") {
    return error.userMessage;
  }
  return "隐私政策操作没有完成，请稍后重试。";
}

function requiredElement(id) {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element: ${id}`);
  }
  return element;
}

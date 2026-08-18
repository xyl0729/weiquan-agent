import { getCaptchaConfig } from "./api.js";

const CAPTCHA_SCRIPT_URL =
  "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js";
let scriptPromise = null;

export class CaptchaPendingError extends Error {
  constructor(message = "请先完成人机验证。") {
    super(message);
    this.name = "CaptchaPendingError";
    this.userMessage = message;
  }
}

export async function loadCaptchaConfig() {
  return getCaptchaConfig();
}

export function createCaptchaController({
  slotId,
  buttonId,
  statusId,
}) {
  const slot = requiredElement(slotId);
  const button = requiredElement(buttonId);
  const status = requiredElement(statusId);
  let config = null;
  let state = "loading";
  let token = "";
  let instance = null;
  let requestedVisible = false;

  return {
    async configure(nextConfig) {
      config = nextConfig;
      token = "";
      instance = null;
      syncVisibility();
      if (!config.enabled) {
        state = "ready";
        slot.dataset.status = "disabled";
        button.hidden = true;
        status.textContent = "";
        return;
      }

      state = "loading";
      slot.dataset.status = "loading";
      button.hidden = false;
      button.disabled = true;
      status.textContent = "正在加载人机验证";
      try {
        await loadCaptchaScript();
        initializeAliyunCaptcha();
      } catch {
        state = "error";
        slot.dataset.status = "error";
        button.disabled = true;
        status.textContent = "人机验证暂时无法加载";
      }
    },
    consumeToken() {
      if (!config) {
        throw new CaptchaPendingError("人机验证仍在初始化，请稍后重试。");
      }
      if (!config.enabled) {
        return null;
      }
      if (state === "error") {
        throw new CaptchaPendingError("人机验证暂时不可用，请稍后重试。");
      }
      if (!token) {
        button.focus();
        throw new CaptchaPendingError();
      }
      const result = token;
      token = "";
      state = "ready";
      slot.dataset.status = "ready";
      status.textContent = "请完成人机验证";
      instance?.reset?.();
      return result;
    },
    reset() {
      token = "";
      if (config?.enabled && state !== "error") {
        state = "ready";
        slot.dataset.status = "ready";
        status.textContent = "请完成人机验证";
        instance?.reset?.();
      }
    },
    isAvailable() {
      return state !== "error";
    },
    isEnabled() {
      return Boolean(config?.enabled);
    },
    setVisible(visible) {
      requestedVisible = Boolean(visible);
      syncVisibility();
    },
  };

  function syncVisibility() {
    slot.hidden = !requestedVisible || !config?.enabled;
  }

  function initializeAliyunCaptcha() {
    if (typeof window.initAliyunCaptcha !== "function") {
      throw new Error("Aliyun CAPTCHA initializer is unavailable");
    }
    window.AliyunCaptchaConfig = {
      region: "cn",
      prefix: config.prefix,
    };
    window.initAliyunCaptcha({
      SceneId: config.scene_id,
      prefix: config.prefix,
      mode: "popup",
      element: `#${slotId}`,
      button: `#${buttonId}`,
      success(captchaVerifyParam) {
        token = String(captchaVerifyParam || "");
        state = token ? "verified" : "ready";
        slot.dataset.status = state;
        status.textContent = token
          ? "人机验证已通过"
          : "请重新完成人机验证";
      },
      fail() {
        token = "";
        state = "ready";
        slot.dataset.status = "ready";
        status.textContent = "验证未通过，请重试";
      },
      getInstance(value) {
        instance = value;
        state = "ready";
        slot.dataset.status = "ready";
        button.disabled = false;
        status.textContent = "请完成人机验证";
      },
    });
  }
}

function loadCaptchaScript() {
  if (typeof window.initAliyunCaptcha === "function") {
    return Promise.resolve();
  }
  if (scriptPromise !== null) {
    return scriptPromise;
  }
  scriptPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = CAPTCHA_SCRIPT_URL;
    script.async = true;
    script.addEventListener("load", resolve, { once: true });
    script.addEventListener(
      "error",
      () => reject(new Error("CAPTCHA script failed to load")),
      { once: true },
    );
    document.head.append(script);
  });
  return scriptPromise;
}

function requiredElement(id) {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element: ${id}`);
  }
  return element;
}

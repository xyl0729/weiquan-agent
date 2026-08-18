import {
  ApiError,
  loginAccount,
  logoutAccount,
  registerAccount,
  requestPasswordReset,
  resendVerification,
  resetPassword,
  verifyEmail,
} from "./api.js";
import { hasRegisteredAccount } from "./capabilities.js";

export function consumeAuthFragment() {
  const fragment = window.location.hash;
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
  const params = new URLSearchParams(
    fragment.startsWith("#") ? fragment.slice(1) : fragment,
  );
  const action = params.get("action");
  const token = params.get("token");
  if (
    action !== "reset-password" ||
    typeof token !== "string" ||
    token.length < 16
  ) {
    return null;
  }
  return { action, token };
}

export function discardAuthFragment() {
  if (!window.location.hash) {
    return;
  }
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
}

export function createAuthController({
  captcha,
  privacy,
  getIdentity,
  onAuthenticated,
  onLoggedOut,
  onStatus,
}) {
  const dialog = requiredElement("auth-dialog");
  const form = requiredElement("auth-form");
  const close = requiredElement("auth-close");
  const title = requiredElement("auth-title");
  const description = requiredElement("auth-description");
  const tabs = requiredElement("auth-tabs");
  const loginTab = requiredElement("auth-login-tab");
  const registerTab = requiredElement("auth-register-tab");
  const emailField = requiredElement("auth-email-field");
  const email = requiredElement("auth-email");
  const passwordField = requiredElement("auth-password-field");
  const passwordLabel = requiredElement("auth-password-label");
  const password = requiredElement("auth-password");
  const passwordHint = requiredElement("auth-password-hint");
  const passwordToggle = requiredElement("auth-password-toggle");
  const passwordVisibilityIcon = requiredElement(
    "auth-password-visibility-icon",
  );
  const codeField = requiredElement("auth-code-field");
  const code = requiredElement("auth-code");
  const privacyField = requiredElement("auth-privacy-field");
  const privacyAccept = requiredElement("auth-privacy-accept");
  const privacyVersion = requiredElement("auth-privacy-version");
  const privacyOpen = requiredElement("auth-privacy-open");
  const error = requiredElement("auth-error");
  const submit = requiredElement("auth-submit");
  const forgot = requiredElement("auth-forgot");
  const resend = requiredElement("auth-resend");
  const back = requiredElement("auth-back");
  const logout = requiredElement("auth-logout");
  const accountButton = requiredElement("account-button");
  let mode = "login";
  let resetToken = "";
  let pendingEmail = "";
  let busy = false;
  let resendAvailableAt = 0;
  let resendTimer = null;

  accountButton.addEventListener("click", () => {
    const identity = getIdentity();
    if (["local", "loading", "unavailable"].includes(identity.status)) {
      return;
    }
    if (hasRegisteredAccount(identity)) {
      open("account");
    } else if (identity.status === "pending_verification") {
      pendingEmail = identity.pendingEmail || pendingEmail;
      open("pending");
    } else {
      open("login");
    }
  });
  loginTab.addEventListener("click", () => setMode("login"));
  registerTab.addEventListener("click", () => setMode("register"));
  forgot.addEventListener("click", () => setMode("forgot"));
  back.addEventListener("click", () => setMode("login"));
  resend.addEventListener("click", () => void resendEmail());
  code.addEventListener("input", () => {
    code.value = code.value.replace(/\D/g, "").slice(0, 6);
  });
  logout.addEventListener("click", () => void performLogout());
  passwordToggle.addEventListener("click", togglePasswordVisibility);
  close.addEventListener("click", closeDialog);
  dialog.addEventListener("cancel", (event) => {
    if (busy) {
      event.preventDefault();
      return;
    }
    closeDialog();
  });
  privacyOpen.addEventListener("click", () => {
    void privacy.showPolicy(privacyOpen).catch(() => {});
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitForm();
  });

  return {
    open,
    async handleFragment(fragmentAction) {
      if (!fragmentAction || fragmentAction.action !== "reset-password") {
        return;
      }
      resetToken = fragmentAction.token;
      open("reset");
    },
  };

  function open(nextMode = "login") {
    setMode(nextMode);
    if (!dialog.open) {
      dialog.showModal();
    }
    requestAnimationFrame(() => {
      if (nextMode === "account") {
        logout.focus();
      } else if (nextMode === "pending") {
        code.focus();
      } else {
        (emailField.hidden ? password : email).focus();
      }
    });
  }

  function closeDialog() {
    if (dialog.open && !busy) {
      dialog.close();
    }
  }

  function setMode(nextMode) {
    mode = nextMode;
    error.textContent = "";
    password.value = "";
    setPasswordVisibility(false);
    captcha.reset();
    const formMode = [
      "login",
      "register",
      "forgot",
      "reset",
      "pending",
    ].includes(mode);
    form.hidden = false;
    tabs.hidden = !["login", "register"].includes(mode);
    emailField.hidden = !["login", "register", "forgot"].includes(
      mode,
    );
    passwordField.hidden = !["login", "register", "reset"].includes(
      mode,
    );
    codeField.hidden = mode !== "pending";
    passwordHint.hidden = !["register", "reset"].includes(mode);
    privacyField.hidden = mode !== "register";
    captcha.setVisible(mode === "register");
    forgot.hidden = mode !== "login";
    resend.hidden = mode !== "pending";
    back.hidden = !["forgot", "reset", "pending"].includes(mode);
    logout.hidden = mode !== "account";
    submit.hidden = !formMode;
    updateResendButton();
    loginTab.dataset.active = String(mode === "login");
    registerTab.dataset.active = String(mode === "register");
    loginTab.setAttribute(
      "aria-selected",
      String(mode === "login"),
    );
    registerTab.setAttribute(
      "aria-selected",
      String(mode === "register"),
    );

    const policy = privacy.getPolicy();
    privacyVersion.textContent = policy
      ? `版本 ${policy.version}`
      : "当前版本";
    if (mode === "register") {
      title.textContent = "注册公测账号";
      description.textContent =
        "验证邮箱后可使用历史、附件和独立注册额度。";
      passwordLabel.textContent = "密码";
      password.autocomplete = "new-password";
      password.placeholder = "创建至少 8 个字符的密码";
      submit.textContent = "注册并发送验证码";
    } else if (mode === "forgot") {
      title.textContent = "找回密码";
      description.textContent =
        "如邮箱存在，将收到 30 分钟内有效的重置链接。";
      submit.textContent = "发送重置邮件";
    } else if (mode === "reset") {
      title.textContent = "设置新密码";
      description.textContent = "新密码长度为 8 至 128 个字符。";
      passwordLabel.textContent = "新密码";
      password.autocomplete = "new-password";
      password.placeholder = "输入新的登录密码";
      submit.textContent = "更新密码";
    } else if (mode === "pending") {
      title.textContent = "输入邮箱验证码";
      description.textContent = pendingEmail
        ? `验证码已发送至 ${pendingEmail}`
        : "请输入邮件中的 6 位验证码。";
      submit.textContent = "验证邮箱";
    } else if (mode === "account") {
      const identity = getIdentity();
      title.textContent = "账号";
      description.textContent = identity.user?.email || "已登录";
    } else {
      title.textContent = "登录账号";
      description.textContent =
        "登录后可查看历史、上传材料并使用注册额度。";
      passwordLabel.textContent = "密码";
      password.autocomplete = "current-password";
      password.placeholder = "请输入密码";
      submit.textContent = "登录";
    }
  }

  function togglePasswordVisibility() {
    setPasswordVisibility(password.type === "password");
    password.focus();
  }

  function setPasswordVisibility(visible) {
    password.type = visible ? "text" : "password";
    passwordToggle.setAttribute("aria-pressed", String(visible));
    const label = visible ? "隐藏密码" : "显示密码";
    passwordToggle.setAttribute("aria-label", label);
    passwordToggle.dataset.tooltip = label;
    passwordVisibilityIcon.setAttribute(
      "href",
      `/static/icons/lucide.svg#${visible ? "eye-off" : "eye"}`,
    );
  }

  async function submitForm() {
    const normalizedEmail = email.value.trim();
    const enteredPassword = password.value;
    if (
      !["reset", "pending"].includes(mode) &&
      !normalizedEmail
    ) {
      error.textContent = "请输入邮箱地址。";
      email.focus();
      return;
    }
    if (
      ["login", "register", "reset"].includes(mode) &&
      (enteredPassword.length < 8 || enteredPassword.length > 128)
    ) {
      error.textContent = "密码长度必须为 8 至 128 个字符。";
      password.focus();
      return;
    }
    if (mode === "pending" && !/^[0-9]{6}$/.test(code.value)) {
      error.textContent = "请输入邮件中的 6 位数字验证码。";
      code.focus();
      return;
    }
    submit.disabled = true;
    submit.dataset.loading = "true";
    busy = true;
    error.textContent = "";
    try {
      if (mode === "login") {
        const projection = await loginAccount({
          email: normalizedEmail,
          password: enteredPassword,
        });
        await onAuthenticated(projection);
        dialog.close();
      } else if (mode === "register") {
        if (!privacyAccept.checked) {
          throw new AuthFormError("请先确认当前隐私政策。");
        }
        const policy = privacy.getPolicy();
        if (!policy) {
          throw new AuthFormError("隐私政策仍在加载，请稍后重试。");
        }
        const captchaToken = captcha.consumeToken();
        await registerAccount({
          email: normalizedEmail,
          password: enteredPassword,
          captchaToken,
          privacyVersion: policy.version,
        });
        pendingEmail = normalizedEmail;
        onStatus("pending_verification", {
          pendingEmail,
        });
        code.value = "";
        startResendCountdown();
        setMode("pending");
      } else if (mode === "forgot") {
        await requestPasswordReset(normalizedEmail);
        setMode("login");
        description.textContent =
          "如邮箱存在，重置邮件已经发送，请检查收件箱。";
      } else if (mode === "reset") {
        await resetPassword({
          token: resetToken,
          newPassword: enteredPassword,
        });
        resetToken = "";
        setMode("login");
        description.textContent = "密码已更新，请使用新密码登录。";
      } else if (mode === "pending") {
        await verifyEmail({
          email: pendingEmail,
          code: code.value,
        });
        stopResendCountdown();
        code.value = "";
        email.value = pendingEmail;
        onStatus("trial", { pendingEmail: "" });
        setMode("login");
        description.textContent = "邮箱验证成功，请登录账号。";
      }
    } catch (submitError) {
      if (
        submitError instanceof ApiError &&
        submitError.code === "registration_capacity_full"
      ) {
        onStatus("capacity_full");
      }
      error.textContent = safeErrorMessage(submitError);
      captcha.reset();
    } finally {
      busy = false;
      submit.disabled = false;
      submit.dataset.loading = "false";
    }
  }

  async function resendEmail() {
    if (!pendingEmail || busy) {
      return;
    }
    busy = true;
    resend.disabled = true;
    error.textContent = "";
    try {
      await resendVerification(pendingEmail);
      description.textContent =
        `新验证码已发送至 ${pendingEmail}`;
      code.value = "";
      startResendCountdown();
    } catch (resendError) {
      error.textContent = safeErrorMessage(resendError);
    } finally {
      busy = false;
      updateResendButton();
    }
  }

  function startResendCountdown(seconds = 60) {
    stopResendCountdown();
    resendAvailableAt = Date.now() + seconds * 1000;
    updateResendButton();
    resendTimer = window.setInterval(updateResendButton, 250);
  }

  function stopResendCountdown() {
    if (resendTimer !== null) {
      window.clearInterval(resendTimer);
      resendTimer = null;
    }
    resendAvailableAt = 0;
    updateResendButton();
  }

  function updateResendButton() {
    const remaining = Math.max(
      0,
      Math.ceil((resendAvailableAt - Date.now()) / 1000),
    );
    resend.disabled = busy || remaining > 0;
    resend.textContent =
      remaining > 0
        ? `重新发送（${remaining} 秒）`
        : "重新发送验证码";
    if (remaining === 0 && resendTimer !== null) {
      window.clearInterval(resendTimer);
      resendTimer = null;
    }
  }

  async function performLogout() {
    if (busy) {
      return;
    }
    busy = true;
    logout.disabled = true;
    error.textContent = "";
    try {
      await logoutAccount();
      await onLoggedOut();
      dialog.close();
    } catch (logoutError) {
      error.textContent = safeErrorMessage(logoutError);
    } finally {
      logout.disabled = false;
      busy = false;
    }
  }
}

class AuthFormError extends Error {
  constructor(userMessage) {
    super(userMessage);
    this.userMessage = userMessage;
  }
}

function safeErrorMessage(error) {
  if (error instanceof ApiError) {
    return error.userMessage;
  }
  if (error && typeof error.userMessage === "string") {
    return error.userMessage;
  }
  return "账号操作没有完成，请稍后重试。";
}

function requiredElement(id) {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element: ${id}`);
  }
  return element;
}

import {
  hasRegisteredAccount,
  hasWorkspaceAccess,
} from "./capabilities.js";

const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const STATUS_LABELS = {
  need_more_facts: "等待补充",
  ready: "方案已整理",
  escalate: "建议专业核对",
};
const ATTACHMENT_STATUS_LABELS = {
  processing: "正在本机提取",
  review_required: "等待人工核对",
  confirmed: "已确认，可随本轮发送",
  failed: "处理未完成",
  bound: "已用于本轮咨询",
};
const ATTACHMENT_WARNING_LABELS = {
  low_confidence: "部分文字识别置信度较低，请重点核对。",
  review_amount: "请重点核对金额、日期和编号。",
};
const MAX_ATTACHMENT_CONTEXT_LENGTH = 12000;
const LOW_CONFIDENCE_THRESHOLD = 0.75;
const LEGACY_UNVERIFIED_LIMITATION =
  "该主题尚未经过本项目的本地法条与确定性规则核验";
const GENERAL_GUIDANCE_LIMITATION =
  "当前提供的是该类问题的一般处理建议，具体责任和适用规则仍需结合事实与证据进一步核对。";

export function createRenderer(actions) {
  const refs = collectRefs();
  let previous = {};

  return (state) => {
    renderShell(refs, state);
    renderService(refs, state.service);
    renderIdentity(refs, state);
    renderNewComposer(refs, state);
    renderCaseComposer(refs, state);
    if (
      state.attachments.new !== previous.attachments?.new ||
      state.attachments.ocr !== previous.attachments?.ocr ||
      state.busy.consult !== previous.busy?.consult
    ) {
      renderAttachmentGroup(refs, state, "new", actions);
    }
    if (
      state.attachments.case !== previous.attachments?.case ||
      state.attachments.ocr !== previous.attachments?.ocr ||
      state.busy.consult !== previous.busy?.consult ||
      state.busy.session !== previous.busy?.session ||
      state.sessionExpired !== previous.sessionExpired
    ) {
      renderAttachmentGroup(refs, state, "case", actions);
    }
    renderAttachmentReview(
      refs,
      state,
      previous.attachments?.review || null,
    );
    renderCaseHeader(refs, state);
    renderCaseAlert(refs, state, actions);

    if (
      state.history !== previous.history ||
      state.currentSessionId !== previous.currentSessionId ||
      state.busy.delete !== previous.busy?.delete
    ) {
      renderHistory(refs, state, actions);
    }
    if (
      state.turns !== previous.turns ||
      state.busy.session !== previous.busy?.session
    ) {
      renderThread(refs, state, actions);
      renderSummary(refs, state);
    }
    if (state.toast !== previous.toast) {
      renderToast(refs, state.toast);
    }
    previous = state;
  };
}

function collectRefs() {
  return {
    app: requiredElement("app"),
    headerPath: requiredElement("header-path"),
    historyToggle: requiredElement("history-toggle"),
    historyRetry: requiredElement("history-retry"),
    drawerBackdrop: requiredElement("drawer-backdrop"),
    historyStatus: requiredElement("history-status"),
    historyList: requiredElement("history-list"),
    serviceState: requiredElement("service-state"),
    serviceStateLabel: requiredElement("service-state-label"),
    accountButton: requiredElement("account-button"),
    accountSummary: requiredElement("account-summary"),
    quotaSummary: requiredElement("quota-summary"),
    newScreen: requiredElement("new-screen"),
    newForm: requiredElement("new-form"),
    newMessage: requiredElement("new-message"),
    newMessageError: requiredElement("new-message-error"),
    newCharacterCount: requiredElement("new-character-count"),
    newSend: requiredElement("new-send"),
    newAttachmentList: requiredElement("new-attachment-list"),
    newAttachmentBlocker: requiredElement("new-attachment-blocker"),
    newAttachmentTrigger: requiredElement("new-attachment-trigger"),
    caseScreen: requiredElement("case-screen"),
    caseNumber: requiredElement("case-number"),
    caseTitle: requiredElement("case-title"),
    caseStage: requiredElement("case-stage"),
    caseStageLabel: requiredElement("case-stage-label"),
    caseAlert: requiredElement("case-alert"),
    thread: requiredElement("thread"),
    caseForm: requiredElement("case-form"),
    caseMessage: requiredElement("case-message"),
    caseMessageError: requiredElement("case-message-error"),
    caseCharacterCount: requiredElement("case-character-count"),
    caseSend: requiredElement("case-send"),
    caseAttachmentList: requiredElement("case-attachment-list"),
    caseAttachmentBlocker: requiredElement("case-attachment-blocker"),
    caseAttachmentTrigger: requiredElement("case-attachment-trigger"),
    caseSummary: requiredElement("case-summary"),
    summaryState: requiredElement("summary-state"),
    summaryContent: requiredElement("summary-content"),
    toastRegion: requiredElement("toast-region"),
    attachmentReviewForm: requiredElement("attachment-review-form"),
    attachmentReviewTitle: requiredElement("attachment-review-title"),
    attachmentReviewDescription: requiredElement(
      "attachment-review-description",
    ),
    attachmentReviewPages: requiredElement("attachment-review-pages"),
    attachmentReviewError: requiredElement("attachment-review-error"),
    attachmentReviewCount: requiredElement("attachment-review-count"),
    attachmentReviewClose: requiredElement("attachment-review-close"),
    attachmentReviewCancel: requiredElement("attachment-review-cancel"),
    attachmentReviewConfirm: requiredElement("attachment-review-confirm"),
  };
}

function renderShell(refs, state) {
  const showingCase = state.view === "case";
  refs.app.dataset.view = showingCase ? "case" : "new";
  refs.app.dataset.historyOpen = String(state.historyOpen);
  refs.historyToggle.setAttribute(
    "aria-expanded",
    String(state.historyOpen),
  );
  refs.drawerBackdrop.tabIndex = state.historyOpen ? 0 : -1;
  refs.newScreen.hidden = showingCase;
  refs.caseScreen.hidden = !showingCase;
  refs.caseSummary.hidden = !showingCase;
  refs.headerPath.textContent = showingCase
    ? state.session?.title || "咨询记录"
    : "新咨询";
}

function renderService(refs, service) {
  refs.serviceState.dataset.status = service.status;
  refs.serviceStateLabel.textContent = service.label;
}

function renderIdentity(refs, state) {
  const identity = state.identity;
  const status = identity.status;
  const quota = identity.quota;
  refs.app.dataset.identity = status;
  refs.accountButton.disabled = ["loading", "local"].includes(status);
  refs.quotaSummary.dataset.exhausted = String(
    identityQuotaExhausted(identity),
  );
  refs.quotaSummary.removeAttribute("title");

  if (status === "local") {
    refs.accountSummary.textContent = "本地完整测试";
    refs.quotaSummary.textContent = "不计应用额度";
    refs.quotaSummary.title =
      "真实模型调用仍可能产生 API 费用。";
    return;
  }
  if (status === "authenticated") {
    refs.accountSummary.textContent =
      identity.user?.email || "已登录";
    refs.quotaSummary.textContent = quota
      ? `今日 ${quota.remaining_daily} · 本月 ${quota.remaining_monthly}`
      : "正在读取额度";
    if (quota) {
      refs.quotaSummary.title = [
        `日额度重置：${formatQuotaReset(quota.day_resets_at)}`,
        `月额度重置：${formatQuotaReset(quota.month_resets_at)}`,
      ].join("；");
    }
    return;
  }
  if (status === "pending_verification") {
    refs.accountSummary.textContent = "验证邮箱";
  } else if (status === "disabled") {
    refs.accountSummary.textContent = "账号已停用";
  } else if (status === "capacity_full") {
    refs.accountSummary.textContent = "公测名额已满";
  } else if (status === "loading") {
    refs.accountSummary.textContent = "正在读取账号";
  } else {
    refs.accountSummary.textContent = "登录 / 注册";
  }

  if (status === "loading") {
    refs.quotaSummary.textContent = "正在读取额度";
  } else if (status === "disabled") {
    refs.quotaSummary.textContent = "咨询暂不可用";
  } else if (quota) {
    refs.quotaSummary.textContent =
      `试用剩余 ${quota.remaining_total} 次`;
  } else {
    refs.quotaSummary.textContent = "匿名试用 5 次";
  }
}

function renderNewComposer(refs, state) {
  const workspaceAccess = hasWorkspaceAccess(state.identity);
  const limit = workspaceAccess ? 4000 : 3000;
  refs.newMessage.maxLength = limit;
  setInputValue(refs.newMessage, state.draftNew);
  refs.newCharacterCount.textContent =
    `${characterLength(state.draftNew)} / ${limit}`;
  refs.newMessageError.textContent = state.inputErrors.new;
  setLoadingButton(refs.newSend, state.busy.consult);
  const identityBlocked =
    !identityCanConsult(state.identity) ||
    identityQuotaExhausted(state.identity);
  refs.newSend.disabled =
    state.busy.consult ||
    identityBlocked ||
    (
      workspaceAccess &&
      attachmentGroupBlocksSend(state.attachments.new)
    );
  refs.newMessage.disabled =
    state.busy.consult ||
    !identityCanConsult(state.identity);
  refs.newForm.setAttribute("aria-busy", String(state.busy.consult));
  for (const starter of document.querySelectorAll(".starter")) {
    starter.disabled = state.busy.consult || identityBlocked;
  }
}

function renderCaseComposer(refs, state) {
  const workspaceAccess = hasWorkspaceAccess(state.identity);
  const limit = workspaceAccess ? 4000 : 3000;
  refs.caseMessage.maxLength = limit;
  setInputValue(refs.caseMessage, state.draftCase);
  refs.caseCharacterCount.textContent =
    `${characterLength(state.draftCase)} / ${limit}`;
  refs.caseMessageError.textContent = state.inputErrors.case;
  const readOnly =
    !identityCanConsult(state.identity) ||
    !state.currentSessionId ||
    state.sessionExpired ||
    state.busy.session;
  const disabled = state.busy.consult || readOnly;
  setLoadingButton(refs.caseSend, state.busy.consult);
  refs.caseSend.disabled =
    disabled ||
    identityQuotaExhausted(state.identity) ||
    (
      workspaceAccess &&
      attachmentGroupBlocksSend(state.attachments.case)
    );
  refs.caseMessage.disabled = disabled;
  refs.caseMessage.placeholder = state.sessionExpired
    ? "这条咨询已过期，请开始新咨询"
    : "继续补充情况";
  refs.caseForm.dataset.readonly = String(readOnly);
  refs.caseForm.setAttribute(
    "aria-busy",
    String(state.busy.consult || state.busy.session),
  );
}

function renderAttachmentGroup(refs, state, source, actions) {
  const group = state.attachments[source];
  const list = refs[`${source}AttachmentList`];
  const blocker = refs[`${source}AttachmentBlocker`];
  const trigger = refs[`${source}AttachmentTrigger`];
  const controlsLocked =
    !hasWorkspaceAccess(state.identity) ||
    state.busy.consult ||
    !group.restored ||
    group.uploading ||
    group.restoring ||
    group.working ||
    Boolean(state.attachments.review?.saving) ||
    (
      source === "case" &&
      (state.busy.session || state.sessionExpired)
    );
  const fragment = document.createDocumentFragment();

  for (const item of group.items) {
    fragment.append(
      renderAttachmentItem(
        item,
        source,
        controlsLocked,
        actions,
      ),
    );
  }
  list.replaceChildren(fragment);

  const reason = attachmentNotice(state, source);
  blocker.hidden = reason === null;
  blocker.textContent = reason?.message || "";
  if (reason) {
    blocker.dataset.tone = reason.tone;
  } else {
    blocker.removeAttribute("data-tone");
  }

  const canUpload =
    state.attachments.ocr.status === "ready" &&
    group.items.length < 3 &&
    !controlsLocked;
  trigger.disabled = !canUpload;
  trigger.dataset.status = state.attachments.ocr.status;
  if (state.attachments.ocr.status === "loading") {
    trigger.setAttribute("aria-label", "正在检查本地文字提取");
  } else if (state.attachments.ocr.status !== "ready") {
    trigger.setAttribute("aria-label", "本地文字提取暂时不可用");
  } else if (group.items.length >= 3) {
    trigger.setAttribute("aria-label", "本轮最多添加 3 个材料");
  } else {
    trigger.setAttribute("aria-label", "添加 PDF 或图片材料");
  }
}

function renderAttachmentItem(
  item,
  source,
  controlsLocked,
  actions,
) {
  const row = document.createElement("div");
  row.className = "attachment-item";
  row.dataset.status = item.status;

  const mark = document.createElement("span");
  mark.className = "attachment-item-mark";
  mark.append(
    item.status === "confirmed"
      ? createIcon("check")
      : (
        item.status === "failed"
          ? createIcon("alert-triangle")
          : createIcon("paperclip")
      ),
  );

  const copy = document.createElement("div");
  copy.className = "attachment-item-copy";
  const name = document.createElement("strong");
  name.textContent = item.original_name || "未恢复的材料";
  const meta = document.createElement("span");
  meta.textContent = [
    attachmentTypeLabel(item),
    formatFileSize(item.size_bytes),
    Number.isInteger(item.page_count)
      ? `${item.page_count} 页`
      : "",
    ATTACHMENT_STATUS_LABELS[item.status] || "状态未知",
  ].filter(Boolean).join(" · ");
  copy.append(name, meta);

  const controls = document.createElement("div");
  controls.className = "attachment-item-actions";
  const key = attachmentKey(item);
  if (
    item.id &&
    ["review_required", "confirmed"].includes(item.status)
  ) {
    const review = document.createElement("button");
    review.className = "attachment-command";
    review.type = "button";
    review.textContent =
      item.status === "confirmed" ? "编辑" : "核对";
    review.disabled = controlsLocked;
    review.addEventListener("click", (event) => {
      actions.reviewAttachment(
        source,
        key,
        event.currentTarget,
      );
    });
    controls.append(review);
  }
  if (item.status === "failed") {
    const recover = document.createElement("button");
    recover.className = "attachment-command";
    recover.type = "button";
    recover.textContent =
      item.can_retry || item.recovery_failed
        ? "重试"
        : "重新选择";
    recover.disabled = controlsLocked;
    recover.addEventListener("click", () => {
      if (item.can_retry || item.recovery_failed) {
        actions.retryAttachment(source, key);
      } else {
        actions.replaceAttachment(source, key);
      }
    });
    controls.append(recover);
  }

  const remove = document.createElement("button");
  remove.className = "icon-button icon-button-small attachment-remove";
  remove.type = "button";
  remove.dataset.tooltip = "移除材料";
  remove.setAttribute(
    "aria-label",
    `移除材料：${item.original_name || "未恢复的材料"}`,
  );
  remove.disabled = controlsLocked;
  remove.append(createIcon("trash-2"));
  remove.addEventListener("click", () => {
    actions.removeAttachment(source, key);
  });
  controls.append(remove);

  row.append(mark, copy, controls);
  const warnings = attachmentWarnings(item);
  if (warnings.length > 0) {
    const warning = document.createElement("p");
    warning.className = "attachment-item-warning";
    warning.textContent = warnings.join(" ");
    row.append(warning);
  }
  return row;
}

function attachmentNotice(state, source) {
  const group = state.attachments[source];
  if (!group.restored || group.restoring) {
    return {
      message: "正在恢复本轮未发送的材料，完成前暂不能发送。",
      tone: "neutral",
    };
  }
  if (group.uploading) {
    return {
      message: "材料正在本机提取文字，完成并核对后才能发送。",
      tone: "neutral",
    };
  }
  if (group.working) {
    return {
      message: "正在更新本轮材料，请稍候。",
      tone: "neutral",
    };
  }
  if (
    group.items.some(
      (item) => item.status === "failed" || item.recovery_failed,
    )
  ) {
    return {
      message: "有材料未处理成功，请重试、重新选择或移除后再发送。",
      tone: "error",
    };
  }
  if (group.items.some((item) => item.status === "review_required")) {
    return {
      message: "请先逐份核对提取文字，确认后才能随本轮发送。",
      tone: "warning",
    };
  }
  if (group.items.some((item) => item.status === "processing")) {
    return {
      message: "材料仍在处理中，请稍候。",
      tone: "neutral",
    };
  }
  if (
    confirmedAttachmentLength(group) >
    MAX_ATTACHMENT_CONTEXT_LENGTH
  ) {
    return {
      message:
        `本轮材料确认文字不能超过 ${MAX_ATTACHMENT_CONTEXT_LENGTH} 个字符。`,
      tone: "error",
    };
  }
  if (group.error) {
    return { message: group.error, tone: "error" };
  }
  if (state.attachments.ocr.status !== "ready") {
    return {
      message:
        state.attachments.ocr.label ||
        "本地文字提取暂时不可用，仍可直接发送文字咨询。",
      tone:
        state.attachments.ocr.status === "loading"
          ? "neutral"
          : "warning",
    };
  }
  return null;
}

function renderAttachmentReview(
  refs,
  state,
  previousReview,
) {
  const review = state.attachments.review;
  if (!review) {
    if (previousReview) {
      refs.attachmentReviewPages.replaceChildren();
      refs.attachmentReviewError.textContent = "";
    }
    return;
  }

  const shouldBuild =
    !previousReview ||
    previousReview.key !== review.key ||
    previousReview.item !== review.item;
  if (shouldBuild) {
    refs.attachmentReviewTitle.textContent =
      review.item.status === "confirmed"
        ? "编辑已确认文字"
        : "核对提取文字";
    refs.attachmentReviewDescription.textContent = [
      review.item.original_name,
      attachmentTypeLabel(review.item),
      formatFileSize(review.item.size_bytes),
      Number.isInteger(review.item.page_count)
        ? `${review.item.page_count} 页`
        : "",
    ].filter(Boolean).join(" · ");
    refs.attachmentReviewPages.replaceChildren(
      createReviewPages(review.item),
    );
    setReviewCharacterCount(
      refs,
      reviewConfirmedLength(state, review),
    );
  }

  const locked = review.saving || state.busy.consult;
  refs.attachmentReviewForm.setAttribute(
    "aria-busy",
    String(review.saving),
  );
  refs.attachmentReviewError.textContent = review.error || "";
  refs.attachmentReviewClose.disabled = locked;
  refs.attachmentReviewCancel.disabled = locked;
  setLoadingButton(refs.attachmentReviewConfirm, review.saving);
  refs.attachmentReviewConfirm.disabled = locked;
  for (const textarea of refs.attachmentReviewPages.querySelectorAll(
    "[data-review-block]",
  )) {
    textarea.disabled = locked;
  }
}

function createReviewPages(item) {
  const fragment = document.createDocumentFragment();
  const groups = reviewGroups(item);
  for (const group of groups) {
    const section = document.createElement("section");
    section.className = "review-page";
    const heading = document.createElement("h3");
    heading.textContent = group.label;
    section.append(heading);

    for (const segment of group.segments) {
      const field = document.createElement("label");
      field.className = "review-block";
      field.dataset.lowConfidence = String(
        segment.confidence < LOW_CONFIDENCE_THRESHOLD,
      );
      const blockHeading = document.createElement("span");
      blockHeading.className = "review-block-heading";
      const blockLabel = document.createElement("strong");
      blockLabel.textContent = `段落 ${segment.index + 1}`;
      const confidence = document.createElement("small");
      confidence.textContent =
        `识别置信度 ${Math.round(segment.confidence * 100)}%`;
      blockHeading.append(blockLabel, confidence);

      const textarea = document.createElement("textarea");
      textarea.dataset.reviewBlock = "";
      textarea.rows = Math.min(
        9,
        Math.max(3, Math.ceil(characterLength(segment.text) / 38)),
      );
      textarea.value = segment.text;
      textarea.setAttribute(
        "aria-label",
        `${group.label}，段落 ${segment.index + 1}`,
      );
      field.append(blockHeading, textarea);

      if (segment.confidence < LOW_CONFIDENCE_THRESHOLD) {
        const notice = document.createElement("span");
        notice.className = "review-low-confidence";
        notice.append(createIcon("alert-triangle"));
        const text = document.createElement("span");
        text.textContent = "这段识别不够清晰，请人工逐字核对。";
        notice.append(text);
        field.append(notice);
      }
      section.append(field);
    }
    fragment.append(section);
  }
  return fragment;
}

function reviewGroups(item) {
  if (item.status === "confirmed" && item.confirmed_text) {
    return [
      {
        label: "已确认内容",
        segments: item.confirmed_text
          .split(/\n\s*\n/u)
          .map((text, index) => ({
            index,
            text,
            confidence: 1,
          })),
      },
    ];
  }

  const pages = new Map();
  const blocks = [...(item.blocks || [])].sort(
    (left, right) =>
      left.page_number - right.page_number ||
      left.block_index - right.block_index,
  );
  for (const block of blocks) {
    if (!pages.has(block.page_number)) {
      pages.set(block.page_number, []);
    }
    pages.get(block.page_number).push({
      index: pages.get(block.page_number).length,
      text: block.text,
      confidence: block.confidence,
    });
  }
  return [...pages.entries()].map(([pageNumber, segments]) => ({
    label: `第 ${pageNumber} 页`,
    segments,
  }));
}

function reviewConfirmedLength(state, review) {
  const group = state.attachments[review.source];
  const otherText = group.items
    .filter(
      (item) =>
        attachmentKey(item) !== review.key &&
        item.status === "confirmed",
    )
    .map((item) => item.confirmed_text || "")
    .join("");
  const currentText = reviewGroups(review.item)
    .flatMap((group) => group.segments)
    .map((segment) => segment.text.trim())
    .filter(Boolean)
    .join("\n\n");
  return characterLength(otherText) + characterLength(currentText);
}

function setReviewCharacterCount(refs, length) {
  refs.attachmentReviewCount.textContent =
    `本轮确认文字 ${length} / ${MAX_ATTACHMENT_CONTEXT_LENGTH}`;
  refs.attachmentReviewCount.dataset.overLimit = String(
    length > MAX_ATTACHMENT_CONTEXT_LENGTH,
  );
}

function renderCaseHeader(refs, state) {
  const latest = latestResponse(state.turns);
  const status =
    latest?.status || state.session?.status || "need_more_facts";
  refs.caseStage.dataset.status = status;
  refs.caseStageLabel.textContent = state.busy.session
    ? "正在读取"
    : responseStatusLabel(latest, status);
  refs.caseNumber.textContent = hasWorkspaceAccess(state.identity)
    ? (
      state.currentSessionId
        ? `案件 ${state.currentSessionId.slice(0, 8).toUpperCase()}`
        : "案件"
    )
    : "匿名试用结果";
  refs.caseTitle.textContent =
    state.session?.title || firstMessageTitle(state.turns) || "咨询记录";
}

function renderCaseAlert(refs, state, actions) {
  refs.caseAlert.replaceChildren();
  const error = state.caseError;
  if (!error) {
    if (
      !hasWorkspaceAccess(state.identity) &&
      state.turns.length > 0
    ) {
      const note = document.createElement("p");
      note.textContent =
        "当前页面内可连续追问，刷新或离开后无法恢复。登录后可保存历史、上传材料并使用独立额度。";
      refs.caseAlert.replaceChildren(note);
      refs.caseAlert.hidden = false;
      refs.caseAlert.dataset.tone = "info";
    } else {
      refs.caseAlert.hidden = true;
      refs.caseAlert.removeAttribute("data-tone");
    }
    return;
  }

  refs.caseAlert.hidden = false;
  refs.caseAlert.dataset.tone = error.tone || "error";
  const message = document.createElement("p");
  message.textContent = error.message;
  refs.caseAlert.append(message);

  if (error.retryable || error.expired) {
    const controls = document.createElement("div");
    controls.className = "case-alert-actions";
    if (error.retryable) {
      const retry = commandButton(
        error.retryLabel || "重试",
        () => actions.retryCase(),
      );
      controls.append(retry);
    }
    if (error.expired) {
      const startNew = commandButton(
        "开始新咨询",
        () => actions.startNew(),
      );
      controls.append(startNew);
    }
    refs.caseAlert.append(controls);
  }
}

function renderHistory(refs, state, actions) {
  const history = state.history;
  refs.historyRetry.disabled = history.refreshing;
  refs.historyRetry.dataset.loading = String(history.refreshing);

  if (history.status === "loading" && history.items.length === 0) {
    setHistoryStatus(refs, "正在读取记录", "loading");
  } else if (history.status === "error") {
    setHistoryStatus(
      refs,
      "记录暂时无法读取，可点击刷新。",
      "error",
    );
  } else if (history.items.length === 0) {
    setHistoryStatus(refs, "还没有咨询记录", "empty");
  } else if (history.refreshing) {
    setHistoryStatus(refs, "正在刷新记录", "loading");
  } else {
    refs.historyStatus.hidden = true;
  }

  const fragment = document.createDocumentFragment();
  for (const session of history.items) {
    const row = document.createElement("div");
    row.className = "history-item";
    row.setAttribute(
      "aria-current",
      String(session.session_id === state.currentSessionId),
    );

    const mark = document.createElement("span");
    mark.className = "history-mark";
    mark.setAttribute("aria-hidden", "true");

    const open = document.createElement("button");
    open.className = "history-item-main";
    open.type = "button";
    open.setAttribute("aria-label", `打开咨询：${session.title}`);
    open.addEventListener("click", () => {
      actions.openSession(session.session_id);
    });

    const copy = document.createElement("span");
    copy.className = "history-item-copy";
    const title = document.createElement("strong");
    title.textContent = session.title;
    const meta = document.createElement("small");
    meta.textContent =
      `${STATUS_LABELS[session.status]} · ${formatHistoryTime(
        session.updated_at,
      )}`;
    copy.append(title, meta);
    open.append(copy);

    const remove = document.createElement("button");
    remove.className = "icon-button history-delete";
    remove.type = "button";
    remove.dataset.tooltip = "删除";
    remove.setAttribute("aria-label", `删除咨询：${session.title}`);
    remove.disabled = state.busy.delete;
    remove.append(createIcon("trash-2"));
    remove.addEventListener("click", () => {
      actions.requestDelete(session);
    });

    row.append(mark, open, remove);
    fragment.append(row);
  }
  refs.historyList.replaceChildren(fragment);
}

function renderThread(refs, state, actions) {
  if (state.busy.session) {
    const loading = document.createElement("div");
    loading.className = "thread-placeholder";
    loading.setAttribute("role", "status");
    loading.textContent = "正在恢复这条咨询…";
    refs.thread.replaceChildren(loading);
    return;
  }
  if (state.turns.length === 0) {
    const empty = document.createElement("div");
    empty.className = "thread-placeholder";
    empty.textContent = "这条咨询还没有可显示的对话。";
    refs.thread.replaceChildren(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  state.turns.forEach((turn, index) => {
    fragment.append(
      renderUserMessage(turn),
      renderAssistantMessage(
        turn,
        index,
        actions,
        hasWorkspaceAccess(state.identity),
      ),
    );
  });
  refs.thread.replaceChildren(fragment);
}

function renderUserMessage(turn) {
  const article = document.createElement("article");
  article.className = "message user-message";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const author = document.createElement("strong");
  author.textContent = "你";
  const time = document.createElement("time");
  time.dateTime = turn.created_at;
  time.textContent = formatMessageTime(turn.created_at);
  meta.append(author, time);
  const body = document.createElement("p");
  body.textContent = turn.user_message;
  article.append(meta, body);
  const attachments = turn.response.attachments || [];
  if (attachments.length > 0) {
    article.append(renderMessageAttachments(attachments));
  }
  return article;
}

function renderMessageAttachments(attachments) {
  const section = document.createElement("section");
  section.className = "message-attachments";
  section.setAttribute("aria-label", "本轮已使用材料");
  const heading = document.createElement("strong");
  heading.className = "message-attachments-heading";
  heading.textContent = `本轮材料 ${attachments.length}`;
  section.append(heading);

  for (const attachment of attachments) {
    const details = document.createElement("details");
    details.className = "message-attachment";
    const summary = document.createElement("summary");
    summary.append(createIcon("paperclip"));
    const copy = document.createElement("span");
    copy.className = "message-attachment-copy";
    const name = document.createElement("strong");
    name.textContent = attachment.original_name;
    const meta = document.createElement("small");
    meta.textContent = [
      attachmentTypeLabel(attachment),
      formatFileSize(attachment.size_bytes),
      Number.isInteger(attachment.page_count)
        ? `${attachment.page_count} 页`
        : "",
    ].filter(Boolean).join(" · ");
    copy.append(name, meta);
    const chevron = createIcon("chevron-down");
    chevron.classList.add("message-attachment-chevron");
    summary.append(copy, chevron);

    const confirmed = document.createElement("p");
    confirmed.className = "message-attachment-text";
    confirmed.textContent = attachment.confirmed_text;
    details.append(summary, confirmed);
    section.append(details);
  }
  return section;
}

function renderAssistantMessage(
  turn,
  index,
  actions,
  allowContinuation,
) {
  const response = turn.response;
  const article = document.createElement("article");
  article.className = "message assistant-message";
  article.dataset.status = response.status;
  article.dataset.turnKind = response.turn_kind;

  const rule = document.createElement("span");
  rule.className = "assistant-rule";
  rule.setAttribute("aria-hidden", "true");
  const content = document.createElement("div");
  content.className = "assistant-content";

  const heading = document.createElement("div");
  heading.className = "assistant-heading";
  const title = document.createElement("strong");
  title.textContent = assistantTitle(response);
  const status = document.createElement("small");
  status.textContent = STATUS_LABELS[response.status];
  heading.append(title, status);
  content.append(heading);

  if (response.turn_kind === "fact_collection") {
    appendReply(content, response.reply, []);
    appendQuestions(content, response.questions);
    appendLimitations(content, response.limitations);
  } else if (
    response.turn_kind === "initial_plan" ||
    response.turn_kind === "plan_update"
  ) {
    if (response.turn_kind === "plan_update") {
      appendPlanUpdateNotice(content);
      if (response.reply) {
        appendReply(content, response.reply, []);
        appendPlanUpdateDetails(content, response);
      } else {
        appendFullPlan(content, response);
      }
    } else {
      appendFullPlan(content, response);
    }
  } else if (response.turn_kind === "followup_answer") {
    appendReply(content, response.reply, response.citations);
    appendLimitations(content, response.limitations);
  } else if (response.turn_kind === "new_case") {
    appendReply(content, response.reply, response.citations);
    if (allowContinuation) {
      appendNewCaseAction(content, turn, actions);
    }
  } else if (
    response.turn_kind === "unverified_guidance" ||
    response.turn_kind === "emergency_guidance"
  ) {
    appendGuidance(content, response);
  }

  article.append(rule, content);
  return article;
}

function appendGuidance(container, response) {
  const guidance = response.guidance;
  if (!guidance) {
    return;
  }
  const emergency = response.turn_kind === "emergency_guidance";
  if (guidance.direct_answer) {
    const answer = document.createElement("p");
    answer.className = "reply-copy";
    answer.textContent = guidance.direct_answer;
    container.append(answer);
  }
  appendResponseSection(
    container,
    emergency ? "现在先确保安全" : "建议先这样处理",
    createTextList(
      guidance.actions,
      "ol",
      emergency
        ? "guidance-action-list emergency-action-list"
        : "guidance-action-list",
    ),
  );
  appendResponseSection(
    container,
    emergency ? "安全情况下再保存这些信息" : "现在保全这些材料",
    createTextList(
      guidance.evidence_now,
      "ul",
      "guidance-evidence-list",
    ),
  );
  appendCommunicationGuide(
    container,
    guidance.communication_guide,
  );
  if (guidance.next_question) {
    const question = document.createElement("p");
    question.className = "guidance-question";
    question.textContent = guidance.next_question;
    appendResponseSection(container, "一个关键问题", question);
  }
  appendLimitations(container, guidance.limitations);
  appendCitations(container, response.citations);
}

function appendPlanUpdateNotice(container) {
  const note = document.createElement("div");
  note.className = "plan-update-note";
  note.append(createIcon("refresh-cw"));
  const text = document.createElement("span");
  text.textContent = "方案已根据新信息更新";
  note.append(text);
  container.append(note);
}

function appendPlanUpdateDetails(container, response) {
  const details = document.createElement("details");
  details.className = "plan-update-details";
  const summary = document.createElement("summary");
  const label = document.createElement("span");
  label.textContent = "查看完整更新方案";
  const chevron = createIcon("chevron-down");
  chevron.classList.add("plan-update-chevron");
  summary.append(label, chevron);

  const body = document.createElement("div");
  body.className = "plan-update-details-body";
  appendFullPlan(body, response);
  details.append(summary, body);
  container.append(details);
}

function appendFullPlan(container, response) {
  appendVerdict(container, response.verdict);
  appendPlan(container, response.plan);
  appendLimitations(
    container,
    uniqueStrings([
      ...response.limitations,
      ...(response.plan?.limitations || []),
      ...(response.plan?.jurisdiction?.notices || []),
    ]),
  );
  appendCitations(container, response.citations);
}

function appendReply(container, reply, citations) {
  if (!reply) {
    return;
  }
  const copy = document.createElement("p");
  copy.className = "reply-copy";
  copy.textContent = reply.text;
  container.append(copy);

  const actions = reply.suggested_actions.slice(0, 3);
  if (actions.length > 0) {
    appendResponseSection(
      container,
      "接下来可以这样做",
      createTextList(actions, "ol", "reply-action-list"),
    );
  }
  appendCitations(container, citations);
}

function appendNewCaseAction(container, turn, actions) {
  const reply = turn.response.reply;
  if (!reply?.new_case) {
    return;
  }
  const block = document.createElement("div");
  block.className = "new-case-block";
  if (reply.new_case.label) {
    const label = document.createElement("small");
    label.textContent = `识别为：${reply.new_case.label}`;
    block.append(label);
  }
  const button = document.createElement("button");
  button.className = "new-case-action";
  button.type = "button";
  const text = document.createElement("span");
  text.textContent = "作为新咨询继续";
  button.append(text, createIcon("arrow-up-right"));
  button.addEventListener("click", () => {
    actions.continueAsNew(
      turn.user_message,
      turn.unbound_attachment_ids || [],
      turn.turn_id,
    );
  });
  block.append(button);
  container.append(block);
}

function appendQuestions(container, questions) {
  if (questions.length === 0) {
    const note = document.createElement("p");
    note.textContent = "现有信息还不足以形成方案，请补充更具体的事实。";
    container.append(note);
    return;
  }
  const list = createTextList(questions, "ol", "question-list");
  container.append(list);
}

function appendVerdict(container, verdict) {
  if (!verdict) {
    return;
  }
  const body = document.createElement("div");
  const label = document.createElement("strong");
  label.className = "response-label";
  label.textContent = verdict.label;
  const point = document.createElement("p");
  point.className = "response-lead";
  point.textContent = verdict.key_point;
  body.append(label, point);
  appendResponseSection(container, "初步判断", body);
}

function appendPlan(container, plan) {
  if (!plan) {
    return;
  }
  const summary = document.createElement("p");
  summary.className = "response-lead";
  summary.textContent = plan.summary;
  appendResponseSection(container, "处理思路", summary);

  if (plan.evidence_now.length > 0) {
    appendResponseSection(
      container,
      "现在保全这些证据",
      createTextList(plan.evidence_now, "ul", "plan-list"),
    );
  }
  if (plan.actions.length > 0) {
    appendResponseSection(
      container,
      "建议按这个顺序处理",
      createTextList(plan.actions, "ol", "plan-list"),
    );
  }
  if (plan.communication_guide) {
    appendCommunicationGuide(container, plan.communication_guide);
  } else if (plan.communication_text) {
    const copy = document.createElement("p");
    copy.className = "communication-copy";
    copy.textContent = plan.communication_text;
    appendResponseSection(container, "历史沟通正文", copy);
  }
  if (plan.time_limit) {
    appendTimeLimit(container, plan.time_limit);
  }
}

function appendCommunicationGuide(container, guide) {
  if (!guide) {
    return;
  }
  const body = document.createElement("div");
  body.className = "communication-guide";

  const context = document.createElement("dl");
  context.className = "communication-context";
  appendDefinitionRow(context, "发送对象", guide.recipient);
  appendDefinitionRow(context, "建议渠道", guide.channels.join("、"));
  appendDefinitionRow(context, "发送时机", guide.when_to_send);
  appendDefinitionRow(context, "沟通目标", guide.objective);
  body.append(context);

  if (guide.required_before_send.length > 0) {
    appendGuideList(
      body,
      "发送前必须补齐",
      guide.required_before_send,
      "required-before-send",
    );
  }

  const messageBlock = document.createElement("div");
  messageBlock.className = "communication-message";
  const messageLabel = document.createElement("strong");
  messageLabel.textContent = "可直接发送的正文";
  const message = document.createElement("p");
  message.textContent = guide.message;
  messageBlock.append(messageLabel, message);
  body.append(messageBlock);

  appendGuideList(body, "发送后保留", guide.after_sending);
  appendGuideList(body, "未解决时升级", guide.escalation);
  appendResponseSection(container, "沟通与发送指南", body);
}

function appendDefinitionRow(container, label, value) {
  const row = document.createElement("div");
  row.className = "communication-row";
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  row.append(term, description);
  container.append(row);
}

function appendGuideList(container, label, items, tone = "normal") {
  const section = document.createElement("div");
  section.className = "guide-list-block";
  section.dataset.tone = tone;
  const heading = document.createElement("strong");
  heading.textContent = label;
  section.append(
    heading,
    createTextList(items, "ul", "guide-list"),
  );
  container.append(section);
}

function appendTimeLimit(container, timeLimit) {
  const body = document.createElement("div");
  body.className = "time-limit";
  const label = document.createElement("strong");
  label.textContent = timeLimit.label;
  const reminder = document.createElement("p");
  reminder.textContent = timeLimit.reminder;
  body.append(label, reminder);
  if (timeLimit.deadline) {
    const deadline = document.createElement("small");
    deadline.textContent = `参考截止日期：${formatPlainDate(
      timeLimit.deadline,
    )}`;
    body.append(deadline);
  }
  appendResponseSection(container, "时效提醒", body);
}

function appendLimitations(container, limitations) {
  const displayLimitations = limitations
    .map(publicLimitationText)
    .filter(Boolean);
  if (displayLimitations.length === 0) {
    return;
  }
  appendResponseSection(
    container,
    "需要留意",
    createTextList(displayLimitations, "ul", "limitation-list"),
  );
}

function publicLimitationText(value) {
  const text = String(value || "").trim();
  const comparable = text.replace(/[。.!！]+$/u, "");
  return comparable === LEGACY_UNVERIFIED_LIMITATION
    ? GENERAL_GUIDANCE_LIMITATION
    : text;
}

function appendCitations(container, citations) {
  if (citations.length === 0) {
    return;
  }
  const list = document.createElement("div");
  list.className = "citation-list";
  citations.forEach((citation, index) => {
    const details = document.createElement("details");
    details.className = "citation";
    const summary = document.createElement("summary");

    const number = document.createElement("span");
    number.className = "citation-number";
    number.textContent = String(index + 1).padStart(2, "0");
    const heading = document.createElement("span");
    heading.className = "citation-heading";
    const law = document.createElement("strong");
    law.textContent = citation.law_name;
    const article = document.createElement("small");
    article.textContent = citation.article_no;
    heading.append(law, article);
    const chevron = createIcon("chevron-down");
    chevron.classList.add("citation-chevron");
    summary.append(number, heading, chevron);

    const body = document.createElement("div");
    body.className = "citation-body";
    const text = document.createElement("p");
    text.textContent = citation.content;
    body.append(text);
    if (citation.applicability_notice) {
      const notice = document.createElement("p");
      notice.className = "citation-notice";
      notice.textContent = citation.applicability_notice;
      body.append(notice);
    }
    const link = createSourceLink(citation.source_url);
    if (link) {
      body.append(link);
    }
    details.append(summary, body);
    list.append(details);
  });
  const generalOnly = citations.every(
    (citation) => citation.basis_scope === "general",
  );
  appendResponseSection(
    container,
    generalOnly ? "该类纠纷的一般法律依据" : "本案法律依据",
    list,
  );
}

function appendResponseSection(container, title, body) {
  const section = document.createElement("section");
  section.className = "response-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading, body);
  container.append(section);
}

function renderSummary(refs, state) {
  if (state.busy.session) {
    refs.summaryState.textContent = "读取中";
    const loading = document.createElement("p");
    loading.className = "summary-empty";
    loading.textContent = "正在恢复案件摘要…";
    refs.summaryContent.replaceChildren(loading);
    return;
  }

  const latest = latestResponse(state.turns);
  const latestPlan = latestPlanResponse(state.turns);
  refs.summaryState.textContent = latest
    ? responseStatusLabel(latest, latest.status)
    : "整理中";

  if (isGuidanceResponse(latest)) {
    const fragment = document.createDocumentFragment();
    fragment.append(
      renderGuidanceSummaryProgress(latest),
      renderGuidanceActionsSummary(latest),
      renderGuidanceEvidenceSummary(latest),
      renderGuidanceCommunicationSummary(latest),
      renderGuidanceQuestionSummary(latest),
    );
    refs.summaryContent.replaceChildren(fragment);
    return;
  }

  const fragment = document.createDocumentFragment();
  fragment.append(
    renderSummaryProgress(state.turns, latest, latestPlan),
  );
  fragment.append(
    renderFactsSummary(state.turns, latestPlan),
    renderPendingSummary(latest),
    renderEvidenceSummary(latestPlan),
    renderCitationSummary(latestPlan),
  );
  refs.summaryContent.replaceChildren(fragment);
}

function renderGuidanceSummaryProgress(response) {
  const progress = document.createElement("div");
  progress.className = "summary-progress";
  const completed = [
    Boolean(response.coverage),
    response.guidance.actions.length > 0,
    response.guidance.evidence_now.length > 0,
    Boolean(response.guidance.communication_guide),
  ];
  for (const value of completed) {
    const bar = document.createElement("span");
    bar.dataset.complete = String(value);
    progress.append(bar);
  }
  return progress;
}

function renderGuidanceActionsSummary(response) {
  const emergency =
    response.turn_kind === "emergency_guidance";
  const section = summarySection(
    emergency ? "立即行动" : "优先行动",
  );
  section.dataset.tone = emergency ? "escalate" : "pending";
  section.append(
    createTextList(
      response.guidance.actions,
      "ol",
      "summary-list",
    ),
  );
  return section;
}

function renderGuidanceEvidenceSummary(response) {
  const section = summarySection("证据与记录");
  section.append(
    createTextList(
      response.guidance.evidence_now,
      "ul",
      "summary-list",
    ),
  );
  return section;
}

function renderGuidanceCommunicationSummary(response) {
  const guide = response.guidance.communication_guide;
  const section = summarySection("沟通重点");
  section.append(
    summaryRow("发送对象", guide.recipient),
    summaryRow("沟通目标", guide.objective),
    summaryRow("建议时机", guide.when_to_send),
  );
  return section;
}

function renderGuidanceQuestionSummary(response) {
  const section = summarySection("关键补充");
  const question = response.guidance.next_question;
  section.append(
    question
      ? summaryRow("下一问题", question, "pending")
      : summaryEmpty("当前以安全行动和现有指导为先。"),
  );
  return section;
}

function renderSummaryProgress(turns, latest, latestPlan) {
  const progress = document.createElement("div");
  progress.className = "summary-progress";
  const completed = [
    turns.length > 0,
    Boolean(latest && latest.questions.length === 0),
    Boolean(latestPlan?.plan?.evidence_now.length),
    Boolean(latestPlan?.citations.length),
  ];
  for (const value of completed) {
    const bar = document.createElement("span");
    bar.dataset.complete = String(value);
    progress.append(bar);
  }
  return progress;
}

function renderFactsSummary(turns, latestPlan) {
  const section = summarySection("案情事实");
  if (latestPlan?.plan) {
    section.append(summaryRow("方案摘要", latestPlan.plan.summary));
    if (latestPlan.verdict?.key_point) {
      section.append(
        summaryRow("判断要点", latestPlan.verdict.key_point),
      );
    }
    return section;
  }
  if (turns.length === 0) {
    section.append(summaryEmpty("尚无可整理的案情。"));
    return section;
  }
  turns.forEach((turn, index) => {
    section.append(
      summaryRow(
        `第 ${index + 1} 次陈述`,
        truncateText(turn.user_message, 88),
      ),
    );
  });
  return section;
}

function renderPendingSummary(latest) {
  const section = summarySection("待补充");
  if (!latest) {
    section.append(summaryEmpty("等待第一轮咨询结果。"));
  } else if (latest.questions.length === 0) {
    section.append(summaryEmpty("当前没有待补充项。"));
  } else {
    section.append(
      createTextList(latest.questions, "ul", "summary-list"),
    );
  }
  return section;
}

function renderEvidenceSummary(latestPlan) {
  const section = summarySection("证据");
  const evidence = latestPlan?.plan?.evidence_now || [];
  if (evidence.length === 0) {
    section.append(summaryEmpty("形成方案后在这里列出证据。"));
  } else {
    section.append(createTextList(evidence, "ul", "summary-list"));
  }
  return section;
}

function renderCitationSummary(latestPlan) {
  const section = summarySection("法律依据");
  const citations = latestPlan?.citations || [];
  if (citations.length === 0) {
    section.append(summaryEmpty("尚无可展示的法律依据。"));
    return section;
  }
  citations.forEach((citation, index) => {
    const row = document.createElement("div");
    row.className = "summary-citation";
    const number = document.createElement("span");
    number.textContent = String(index + 1).padStart(2, "0");
    const copy = document.createElement("div");
    copy.className = "summary-citation-copy";
    const law = document.createElement("strong");
    law.textContent = `${citation.law_name} ${citation.article_no}`;
    copy.append(law);
    const link = createSourceLink(citation.source_url, "查看来源");
    if (link) {
      copy.append(link);
    }
    row.append(number, copy);
    section.append(row);
  });
  return section;
}

function summarySection(title) {
  const section = document.createElement("section");
  section.className = "summary-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading);
  return section;
}

function summaryRow(label, value, tone = "formal") {
  const row = document.createElement("div");
  row.className = "summary-row";
  const signal = document.createElement("span");
  signal.className = "summary-signal";
  signal.dataset.tone = tone;
  signal.setAttribute("aria-hidden", "true");
  const copy = document.createElement("div");
  copy.className = "summary-copy";
  const small = document.createElement("small");
  small.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = value;
  copy.append(small, strong);
  row.append(signal, copy);
  return row;
}

function summaryEmpty(message) {
  const paragraph = document.createElement("p");
  paragraph.className = "summary-empty";
  paragraph.textContent = message;
  return paragraph;
}

function renderToast(refs, toast) {
  refs.toastRegion.replaceChildren();
  if (!toast) {
    return;
  }
  const message = document.createElement("div");
  message.className = "toast";
  message.dataset.tone = toast.tone;
  message.setAttribute("role", toast.tone === "error" ? "alert" : "status");
  message.textContent = toast.message;
  refs.toastRegion.append(message);
}

function setHistoryStatus(refs, text, state) {
  refs.historyStatus.hidden = false;
  refs.historyStatus.dataset.state = state;
  refs.historyStatus.textContent = text;
}

function setLoadingButton(button, loading) {
  button.disabled = loading;
  button.dataset.loading = String(loading);
}

function setInputValue(input, value) {
  if (input.value !== value) {
    input.value = value;
  }
}

function commandButton(label, handler) {
  const button = document.createElement("button");
  button.className = "inline-action";
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function createTextList(items, tagName, className) {
  const list = document.createElement(tagName);
  list.className = className;
  for (const item of items) {
    const row = document.createElement("li");
    row.textContent = item;
    list.append(row);
  }
  return list;
}

function createSourceLink(sourceUrl, label = "查看权威来源") {
  const safeUrl = safeSourceUrl(sourceUrl);
  if (!safeUrl) {
    return null;
  }
  const link = document.createElement("a");
  link.className = "source-link";
  link.href = safeUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = label;
  link.append(createIcon("external-link"));
  return link;
}

function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol === "http:" || url.protocol === "https:") {
      return url.href;
    }
  } catch {
    return null;
  }
  return null;
}

function createIcon(name) {
  const svg = document.createElementNS(SVG_NAMESPACE, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NAMESPACE, "use");
  use.setAttribute("href", `/static/icons/lucide.svg#${name}`);
  svg.append(use);
  return svg;
}

function attachmentKey(item) {
  return item.ui_key || item.id || "";
}

function attachmentTypeLabel(item) {
  const mediaType = item.media_type;
  if (mediaType === "application/pdf") {
    return "PDF";
  }
  if (mediaType === "image/png") {
    return "PNG";
  }
  if (mediaType === "image/jpeg") {
    return "JPEG";
  }
  const name = String(item.original_name || "").toLowerCase();
  if (name.endsWith(".pdf")) {
    return "PDF";
  }
  if (name.endsWith(".png")) {
    return "PNG";
  }
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) {
    return "JPEG";
  }
  return "材料";
}

function formatFileSize(value) {
  if (!Number.isInteger(value) || value < 0) {
    return "";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${Math.max(1, Math.round(value / 1024))} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function attachmentWarnings(item) {
  const warnings = [];
  if (
    (item.blocks || []).some(
      (block) => block.confidence < LOW_CONFIDENCE_THRESHOLD,
    )
  ) {
    warnings.push("部分段落识别置信度较低，请在核对窗口逐字检查。");
  }
  for (const warning of item.warnings || []) {
    if (ATTACHMENT_WARNING_LABELS[warning]) {
      warnings.push(ATTACHMENT_WARNING_LABELS[warning]);
    } else if (warning !== "low_confidence") {
      warnings.push("提取结果包含需要人工核对的内容。");
    }
  }
  if (item.ui_error) {
    warnings.push(item.ui_error);
  }
  return [...new Set(warnings)];
}

function confirmedAttachmentLength(group) {
  return characterLength(
    group.items
      .filter((item) => item.status === "confirmed")
      .map((item) => item.confirmed_text || "")
      .join(""),
  );
}

function attachmentGroupBlocksSend(group) {
  if (!group) {
    return true;
  }
  if (
    !group.restored ||
    group.restoring ||
    group.uploading ||
    group.working
  ) {
    return true;
  }
  if (confirmedAttachmentLength(group) > MAX_ATTACHMENT_CONTEXT_LENGTH) {
    return true;
  }
  return group.items.some(
    (item) => item.status !== "confirmed",
  );
}

function latestResponse(turns) {
  return turns.length > 0 ? turns[turns.length - 1].response : null;
}

function identityCanConsult(identity) {
  return [
    "local",
    "trial",
    "pending_verification",
    "capacity_full",
    "authenticated",
  ].includes(identity.status);
}

function identityQuotaExhausted(identity) {
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

function formatQuotaReset(value) {
  const date = validDate(value);
  if (!date) {
    return "时间未知";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function isGuidanceResponse(response) {
  return Boolean(
    response?.guidance &&
    response?.coverage &&
    (
      response.turn_kind === "unverified_guidance" ||
      response.turn_kind === "emergency_guidance"
    ),
  );
}

function responseStatusLabel(response, fallbackStatus) {
  if (isGuidanceResponse(response)) {
    return response.turn_kind === "emergency_guidance"
      ? "紧急建议已整理"
      : "处理建议已整理";
  }
  return STATUS_LABELS[fallbackStatus] || "整理中";
}

function latestPlanResponse(turns) {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const response = turns[index].response;
    if (response.plan && response.verdict) {
      return response;
    }
  }
  return null;
}

function assistantTitle(response) {
  if (response.turn_kind === "fact_collection") {
    return "还需要确认这些信息";
  }
  if (response.turn_kind === "unverified_guidance") {
    return "针对当前问题的处理建议";
  }
  if (response.turn_kind === "emergency_guidance") {
    return "请先处理紧急风险";
  }
  if (response.turn_kind === "plan_update") {
    return "更新后的维权方案";
  }
  if (response.turn_kind === "followup_answer") {
    return "继续处理";
  }
  if (response.turn_kind === "new_case") {
    return "建议分开咨询";
  }
  if (response.status === "escalate") {
    return "需要进一步核对";
  }
  return response.status === "ready"
    ? "维权方案"
    : "阶段性处理方案";
}

function firstMessageTitle(turns) {
  if (turns.length === 0) {
    return "";
  }
  return truncateText(turns[0].user_message, 24);
}

function truncateText(value, limit) {
  const normalized = String(value).replace(/\s+/g, " ").trim();
  const characters = Array.from(normalized);
  return characters.length <= limit
    ? normalized
    : `${characters.slice(0, limit).join("")}…`;
}

function uniqueStrings(items) {
  return [...new Set(items.filter((item) => item.trim()))];
}

function characterLength(value) {
  return Array.from(value).length;
}

function formatHistoryTime(value) {
  const date = validDate(value);
  if (!date) {
    return "时间未知";
  }
  const now = new Date();
  if (sameLocalDate(date, now)) {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameLocalDate(date, yesterday)) {
    return "昨天";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
  }).format(date);
}

function formatMessageTime(value) {
  const date = validDate(value);
  if (!date) {
    return "时间未知";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatPlainDate(value) {
  const date = validDate(value);
  if (!date) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(date);
}

function validDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function sameLocalDate(left, right) {
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  );
}

function requiredElement(id) {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element: ${id}`);
  }
  return element;
}

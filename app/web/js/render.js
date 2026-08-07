const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const STATUS_LABELS = {
  need_more_facts: "等待补充",
  ready: "方案已整理",
  escalate: "建议专业核对",
};

export function createRenderer(actions) {
  const refs = collectRefs();
  let previous = {};

  return (state) => {
    renderShell(refs, state);
    renderService(refs, state.service);
    renderNewComposer(refs, state);
    renderCaseComposer(refs, state);
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
    newScreen: requiredElement("new-screen"),
    newForm: requiredElement("new-form"),
    newMessage: requiredElement("new-message"),
    newMessageError: requiredElement("new-message-error"),
    newCharacterCount: requiredElement("new-character-count"),
    newSend: requiredElement("new-send"),
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
    caseSummary: requiredElement("case-summary"),
    summaryState: requiredElement("summary-state"),
    summaryContent: requiredElement("summary-content"),
    toastRegion: requiredElement("toast-region"),
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

function renderNewComposer(refs, state) {
  setInputValue(refs.newMessage, state.draftNew);
  refs.newCharacterCount.textContent =
    `${characterLength(state.draftNew)} / 4000`;
  refs.newMessageError.textContent = state.inputErrors.new;
  setLoadingButton(refs.newSend, state.busy.consult);
  refs.newMessage.disabled = state.busy.consult;
  refs.newForm.setAttribute("aria-busy", String(state.busy.consult));
  for (const starter of document.querySelectorAll(".starter")) {
    starter.disabled = state.busy.consult;
  }
}

function renderCaseComposer(refs, state) {
  setInputValue(refs.caseMessage, state.draftCase);
  refs.caseCharacterCount.textContent =
    `${characterLength(state.draftCase)} / 4000`;
  refs.caseMessageError.textContent = state.inputErrors.case;
  const readOnly = state.sessionExpired || state.busy.session;
  const disabled = state.busy.consult || readOnly;
  setLoadingButton(refs.caseSend, state.busy.consult);
  refs.caseSend.disabled = disabled;
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

function renderCaseHeader(refs, state) {
  const latest = latestResponse(state.turns);
  const status =
    latest?.status || state.session?.status || "need_more_facts";
  refs.caseStage.dataset.status = status;
  refs.caseStageLabel.textContent = state.busy.session
    ? "正在读取"
    : STATUS_LABELS[status];
  refs.caseNumber.textContent = state.currentSessionId
    ? `案件 ${state.currentSessionId.slice(0, 8).toUpperCase()}`
    : "案件";
  refs.caseTitle.textContent =
    state.session?.title || firstMessageTitle(state.turns) || "咨询记录";
}

function renderCaseAlert(refs, state, actions) {
  refs.caseAlert.replaceChildren();
  const error = state.caseError;
  if (!error) {
    refs.caseAlert.hidden = true;
    refs.caseAlert.removeAttribute("data-tone");
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
      const retry = commandButton("重试", () => actions.retryCase());
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
      renderAssistantMessage(turn, index, actions),
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
  return article;
}

function renderAssistantMessage(turn, index, actions) {
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
  const round = document.createElement("small");
  round.textContent = `第 ${index + 1} 轮 · ${STATUS_LABELS[response.status]}`;
  heading.append(title, round);
  content.append(heading);

  if (response.turn_kind === "fact_collection") {
    appendQuestions(content, response.questions);
    appendLimitations(content, response.limitations);
  } else if (
    response.turn_kind === "initial_plan" ||
    response.turn_kind === "plan_update"
  ) {
    if (response.turn_kind === "plan_update") {
      appendPlanUpdateNotice(content);
    }
    appendVerdict(content, response.verdict);
    appendPlan(content, response.plan);
    appendLimitations(
      content,
      uniqueStrings([
        ...response.limitations,
        ...(response.plan?.limitations || []),
        ...(response.plan?.jurisdiction?.notices || []),
      ]),
    );
    appendCitations(content, response.citations);
  } else if (response.turn_kind === "followup_answer") {
    appendReply(content, response.reply, response.citations);
    appendLimitations(content, response.limitations);
  } else if (response.turn_kind === "new_case") {
    appendReply(content, response.reply, response.citations);
    appendNewCaseAction(content, turn, actions);
  }

  article.append(rule, content);
  return article;
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
    actions.continueAsNew(turn.user_message);
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
  if (plan.communication_text) {
    const copy = document.createElement("p");
    copy.className = "communication-copy";
    copy.textContent = plan.communication_text;
    appendResponseSection(container, "可直接使用的话术", copy);
  }
  if (plan.time_limit) {
    appendTimeLimit(container, plan.time_limit);
  }
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
  if (limitations.length === 0) {
    return;
  }
  appendResponseSection(
    container,
    "需要留意",
    createTextList(limitations, "ul", "limitation-list"),
  );
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
    const link = createSourceLink(citation.source_url);
    if (link) {
      body.append(link);
    }
    details.append(summary, body);
    list.append(details);
  });
  appendResponseSection(container, "法律依据", list);
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
    ? STATUS_LABELS[latest.status]
    : "整理中";
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

function summaryRow(label, value) {
  const row = document.createElement("div");
  row.className = "summary-row";
  const signal = document.createElement("span");
  signal.className = "summary-signal";
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

function latestResponse(turns) {
  return turns.length > 0 ? turns[turns.length - 1].response : null;
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

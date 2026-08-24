const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const VIEWS = ["today", "curriculum", "history", "campus"];
const CACHE = "become-mobile-v4";
const state = {
  view: VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "today",
  dashboard: null,
  curriculum: null,
  history: [],
  historyCursor: null,
  campus: null,
  revision: null,
  feedback: null,
  retry: null,
  pollTimer: null,
};
let saveTimer = null;
let toastTimer = null;

function cacheKey(name) { return `${CACHE}:${name}`; }
function readJson(key, fallback = null) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function writeJson(key, value) { localStorage.setItem(key, JSON.stringify(value)); }
function token() { return sessionStorage.getItem("become-token") || ""; }
function requestId() { return crypto.randomUUID().replaceAll("-", ""); }
function formatDate(value, withTime = true) {
  if (!value) return "예정 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short", day: "numeric", ...(withTime ? {hour: "numeric", minute: "2-digit"} : {}),
  }).format(date);
}
function humanStatus(value) {
  return ({
    teaching: "설명", review: "복습", artifact: "결과물", session: "세션",
    complete: "확실", partial: "보완 필요", failed: "다시 학습",
    exposure: "새 학습", retrieval: "지연 인출", passed: "통과",
    needs_revision: "수정 필요", draft: "검토 전",
  })[value] || value || "기록";
}
function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 3600);
}
function setConnection(online, detail = "") {
  const node = $("#connection");
  node.textContent = online
    ? "온라인 · 저장 가능"
    : detail === "서버 오류" ? "서버 오류 · 다시 시도" : `오프라인${detail ? ` · ${detail}` : ""}`;
  node.className = `connection ${online ? "online" : "offline"}`;
}
function setBusy(busy) { $("#main").setAttribute("aria-busy", String(busy)); }
function showError(message, retry, replaceView = false) {
  state.retry = retry;
  state.errorReplaced = replaceView;
  if (replaceView) $(`[data-page="${state.view}"]`).hidden = true;
  $("#error-message").textContent = message;
  $("#view-error").hidden = false;
  $("#view-error").focus({preventScroll: true});
}
function clearError() {
  $("#view-error").hidden = true;
  if (state.errorReplaced) $(`[data-page="${state.view}"]`).hidden = false;
  state.errorReplaced = false;
  state.retry = null;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (token()) headers.set("Authorization", `Bearer ${token()}`);
  const response = await fetch(path, {...options, headers, cache: "no-store"});
  let body = {};
  try { body = await response.json(); } catch {}
  if (response.status === 401) {
    sessionStorage.removeItem("become-token");
    $("#auth-panel").hidden = false;
  } else if (response.ok) {
    $("#auth-panel").hidden = true;
  }
  if (!response.ok) {
    const error = new Error(body.error?.message || "요청을 처리하지 못했습니다.");
    error.code = body.error?.code;
    error.status = response.status;
    throw error;
  }
  return body;
}

function attemptFor(item) {
  const key = cacheKey(`attempt:${item.id}:${item.base_sequence}`);
  const existing = readJson(key);
  if (existing) return {...existing, key};
  const attempt = {
    key, request_id: requestId(), knowledge_id: item.id, base_sequence: item.base_sequence,
    title: item.title, answer: "", rating: "", expected_revision: state.revision,
  };
  writeJson(key, attempt);
  return attempt;
}
function saveAttempt(attempt) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => writeJson(attempt.key, attempt), 220);
}
function outbox() { return readJson(cacheKey("outbox"), []); }
function saveOutbox(items) { writeJson(cacheKey("outbox"), items); }
function enqueue(submission) {
  const items = outbox();
  if (!items.some(item => item.request_id === submission.request_id)) items.push(submission);
  saveOutbox(items.slice(-20));
}
function queuedRequest(request) { return outbox().find(item => item.request_id === request); }
function activeJob() {
  const raw = localStorage.getItem(cacheKey("active-job"));
  if (!raw) return null;
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" ? value : null;
  } catch {
    return {id: raw, view: "campus", message: "", replyTo: null};
  }
}
function saveActiveJob(value) { writeJson(cacheKey("active-job"), value); }
function clearActiveJob(id) {
  if (activeJob()?.id === id) localStorage.removeItem(cacheKey("active-job"));
}
function normalizeJob(job) {
  const result = job.result || {};
  return {
    ...job,
    status: job.status || ({
      queued: "학습 준비를 시작했습니다.", running: "학습 경로를 준비하고 있습니다.",
      completed: "다음 단계가 준비되었습니다.", failed: "진행을 이어가지 못했습니다.",
      interrupted: "진행을 중단했습니다.",
    })[job.state],
    response: job.response || result.response || job.error?.message,
    input_request: job.input_request || result.input_request,
    result_revision: job.result_revision || result.result_revision,
  };
}

function list(items, className = "") {
  const node = document.createElement("ul");
  node.className = className;
  for (const value of items) {
    const item = document.createElement("li");
    item.textContent = value;
    node.append(item);
  }
  return node;
}
function paragraph(value, className = "") {
  const node = document.createElement("p");
  node.className = className;
  node.textContent = value;
  return node;
}

function renderToday(data, cached = false) {
  state.dashboard = data;
  state.revision = data.state_revision || state.revision;
  $("#today-goal").textContent = data.goal || data.message || "나의 대학을 시작합니다.";
  const context = $("#today-context");
  context.replaceChildren();
  if (data.current_step) {
    context.append(paragraph(`${data.current_step.order}/${data.current_step.total}단계 · ${data.current_step.title}`, "meta"));
  }
  if (data.previous_confusion) context.append(paragraph(`지난번에 헷갈린 것: ${data.previous_confusion}`));
  else if (data.recent_achievement) context.append(paragraph(data.recent_achievement));
  if (data.progress?.total) {
    const track = document.createElement("div");
    track.className = "progress-track";
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", "커리큘럼 진행");
    track.setAttribute("aria-valuenow", data.progress.completed);
    track.setAttribute("aria-valuemax", data.progress.total);
    const fill = document.createElement("span");
    fill.style.width = `${Math.round(data.progress.completed / data.progress.total * 100)}%`;
    track.append(fill);
    context.append(track);
  }
  context.hidden = !context.childElementCount;

  const content = $("#today-content");
  content.replaceChildren();
  if (data.status === "setup") return renderSetup(content);
  if (!data.current) return renderTodayEmpty(content);
  renderLesson(content, data.current, cached);
}

function renderSetup(container) {
  const section = document.createElement("section");
  section.className = "lesson";
  section.innerHTML = `
    <p class="phase">나의 방향</p>
    <h2 id="lesson-title">어떤 전문가가 되고 싶으세요?</h2>
    <p>한 문장이면 충분합니다. Advisor가 실용 목표와 첫 학습 경로를 정하고, 실제 학습 수행을 보며 조정합니다.</p>
    <form id="setup-form">
      <div class="field"><label for="setup-goal">되고 싶은 모습을 한 문장으로</label><textarea id="setup-goal" required maxlength="1000" placeholder="예: 운영 근거로 분산 시스템 설계를 방어하는 엔지니어"></textarea></div>
      <button class="primary" type="submit">나의 대학 시작하기</button>
    </form>`;
  container.append(section);
  const goalInput = $("#setup-goal", section);
  const goalDraft = cacheKey("setup-goal");
  goalInput.value = localStorage.getItem(goalDraft) || "";
  goalInput.addEventListener("input", () => localStorage.setItem(goalDraft, goalInput.value));
  $("#setup-form", section).addEventListener("submit", async event => {
    event.preventDefault();
    const goal = goalInput.value.trim();
    if (!goal) return;
    const button = $("button", section);
    button.disabled = true;
    const created = await createJob(
      `나는 ${goal}이(가) 되고 싶어. 목표를 되묻지 말고 Advisor가 실용 목표와 첫 학습 경로를 정한 뒤 학습을 시작해줘.`,
      {view: "today"},
    );
    if (!created) button.disabled = false;
  });
}

function renderTodayEmpty(container) {
  const section = document.createElement("section");
  section.className = "empty";
  const title = document.createElement("h2");
  title.id = "lesson-title";
  title.textContent = "지금 만기인 복습은 없습니다.";
  const copy = paragraph("새 지식을 준비하거나 결과물 피드백을 이어갈 수 있습니다.");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.textContent = "다음 학습 준비하기";
  button.addEventListener("click", () => createJob(
    "현재 목표와 학습 증거를 바탕으로 다음에 배울 내용을 준비해줘.",
    {view: "today"},
  ));
  section.append(title, copy, button);
  container.append(section);
}

function renderLesson(container, item, cached) {
  const lesson = document.createElement("article");
  lesson.className = "lesson";
  lesson.setAttribute("aria-labelledby", "lesson-title");
  const head = document.createElement("div");
  head.className = "lesson-head";
  head.append(paragraph(item.phase === "retrieval" ? "지연 인출" : "새 학습 · 설명 먼저", "phase"));
  if (cached || item.phase === "retrieval") {
    head.append(paragraph(cached ? "오프라인 사본" : `만기 ${formatDate(item.due_at)}`, "meta"));
  }
  const title = document.createElement("h2");
  title.id = "lesson-title";
  title.tabIndex = -1;
  title.textContent = item.title;
  lesson.append(head, title);
  let practice = lesson;
  if (item.phase === "exposure") {
    const teaching = document.createElement("section");
    teaching.className = "teaching-flow";
    teaching.append(paragraph(item.explanation, "lesson-intro"));
    for (const label of ["왜 쓰는가", "왜 이렇게 되었는가", "왜 이 결과가 나오는가", "그래서 어디에 쓰는가"]) {
      if (!item.why_chain?.[label]) continue;
      const section = document.createElement("section");
      const heading = document.createElement("h3");
      heading.textContent = label;
      section.append(heading, paragraph(item.why_chain[label]));
      teaching.append(section);
    }
    lesson.append(teaching, paragraph(`이미 아는 것과 연결하면: ${item.connection}`, "connection-note"));
    practice = document.createElement("details");
    practice.className = "practice";
    const summary = document.createElement("summary");
    summary.textContent = "직접 적용해 보기";
    practice.append(summary);
    lesson.append(practice);
  }
  const promptTitle = document.createElement("h3");
  promptTitle.textContent = item.phase === "retrieval" ? "자료 없이 꺼내 보세요" : "다른 상황이라면";
  practice.append(promptTitle, paragraph(item.prompt));
  const attempt = attemptFor(item);
  const form = document.createElement("form");
  form.id = "review-form";
  form.innerHTML = `
    <div class="field"><label for="answer">내 답</label><textarea id="answer" required maxlength="10000" autocomplete="off" aria-describedby="answer-help"></textarea><p id="answer-help" class="field-help">입력 내용은 이 기기에 자동 저장됩니다.</p></div>
    <div class="field"><label for="rating">지금 답에 대한 확신</label><select id="rating" required><option value="">선택하세요</option><option value="again">모름 — 설명이 필요함</option><option value="hard">불확실 — 일부만 설명 가능</option><option value="good">설명 가능 — 핵심을 연결함</option><option value="easy">확실 — 바로 다른 사례에도 적용 가능</option></select></div>
    <div id="queue-status" class="connection" role="status"></div>
    <div class="sticky-action"><button class="primary" type="submit">답을 제출하고 비교하기</button></div>`;
  const answer = $("#answer", form);
  const rating = $("#rating", form);
  answer.value = attempt.answer;
  rating.value = attempt.rating;
  answer.addEventListener("input", () => { attempt.answer = answer.value; saveAttempt(attempt); });
  rating.addEventListener("change", () => { attempt.rating = rating.value; writeJson(attempt.key, attempt); });
  form.addEventListener("submit", event => {
    event.preventDefault();
    attempt.answer = answer.value.trim();
    attempt.rating = rating.value;
    writeJson(attempt.key, attempt);
    if (!attempt.answer || !attempt.rating) return toast("답과 현재 확신을 함께 남겨 주세요.");
    submitReview(attempt);
  });
  practice.append(form);
  if (item.phase === "retrieval") {
    const giveUp = document.createElement("button");
    giveUp.type = "button";
    giveUp.className = "text-action";
    giveUp.textContent = "포기하고 설명 보기";
    giveUp.addEventListener("click", () => {
      attempt.answer = answer.value.trim() || "스스로 답을 만들지 못해 포기함";
      attempt.rating = "again";
      writeJson(attempt.key, attempt);
      submitReview(attempt);
    });
    practice.append(giveUp);
  }
  if (queuedRequest(attempt.request_id)) {
    $("#queue-status", form).textContent = "오프라인 제출 대기 중 · 연결되면 한 번만 동기화합니다.";
  }
  container.append(lesson);
}

async function submitReview(attempt) {
  const form = $("#review-form");
  const button = $("button[type=submit]", form);
  button.disabled = true;
  const submission = {
    knowledge_id: attempt.knowledge_id, answer: attempt.answer, rating: attempt.rating,
    request_id: attempt.request_id, base_sequence: attempt.base_sequence,
    expected_revision: attempt.expected_revision || state.revision,
  };
  if (!navigator.onLine) {
    enqueue(submission);
    button.disabled = false;
    $("#queue-status", form).textContent = "오프라인 제출 대기 중 · 연결되면 한 번만 동기화합니다.";
    return toast("답안을 안전하게 대기열에 저장했습니다.");
  }
  try {
    const result = await api("/api/review", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(submission),
    });
    acceptReview(result, attempt.key);
  } catch (error) {
    if (!error.status) {
      enqueue(submission);
      setConnection(false, "답안 동기화 대기");
      $("#queue-status", form).textContent = "전송이 끊겨 답안을 보관했습니다. 재연결 때 같은 요청으로 확인합니다.";
      toast("답안을 기기에 보관했습니다.");
    } else if (error.status === 409) {
      $("#queue-status", form).textContent = `${error.message} 이 답안은 기기에 남아 있습니다.`;
      showError(error.message, () => loadToday(false));
    } else {
      $("#queue-status", form).textContent = `${error.message} 다시 제출할 수 있습니다.`;
    }
    button.disabled = false;
  }
}

function acceptReview(result, attemptKey) {
  state.revision = result.state_revision;
  state.feedback = result.feedback;
  localStorage.removeItem(attemptKey);
  saveOutbox(outbox().filter(item => item.request_id !== result.request_id));
  renderFeedback(result.feedback);
}

function renderFeedback(feedback) {
  const content = $("#today-content");
  content.replaceChildren();
  const region = document.createElement("section");
  region.className = `feedback ${feedback.first_mismatch ? "answer-gap" : ""}`;
  region.setAttribute("aria-labelledby", "feedback-title");
  const title = document.createElement("h2");
  title.id = "feedback-title";
  title.tabIndex = -1;
  title.textContent = feedback.first_mismatch ? "처음 어긋난 지점부터" : "스스로 꺼냈습니다";
  region.append(title, paragraph(feedback.summary));
  const compare = document.createElement("div");
  compare.className = "answer-compare";
  for (const [heading, text] of [["내가 제출한 답", feedback.answer], ["Tutor의 기준 설명", feedback.reference]]) {
    const block = document.createElement("section");
    const h3 = document.createElement("h3");
    h3.textContent = heading;
    block.append(h3, paragraph(text));
    compare.append(block);
  }
  region.append(compare);
  if (feedback.first_mismatch) {
    const correction = document.createElement("section");
    correction.className = "connection-note";
    const h3 = document.createElement("h3");
    h3.textContent = `첫 차이 · ${feedback.first_mismatch}`;
    correction.append(h3, paragraph(feedback.correction));
    if (feedback.connection) correction.append(paragraph(`연결 단서: ${feedback.connection}`));
    region.append(correction);
  }
  region.append(paragraph(`다음 확인: ${formatDate(feedback.next_due_at)}`, "next-due"));
  const next = document.createElement("button");
  next.type = "button";
  next.className = "primary";
  next.textContent = "다음 학습 보기";
  next.addEventListener("click", () => { state.feedback = null; loadToday(false); });
  region.append(next);
  content.append(region);
  title.focus({preventScroll: true});
  title.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"});
}

async function syncOutbox() {
  const pending = outbox();
  if (!pending.length || !navigator.onLine) return;
  try {
    const body = await api("/api/reviews/sync", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({submissions: pending}),
    });
    const saved = new Set(body.results.filter(item => item.status === "saved").map(item => item.receipt.request_id));
    saveOutbox(pending.filter(item => !saved.has(item.request_id)));
    const current = body.results.find(item => item.status === "saved" && state.dashboard?.current?.id === item.receipt.knowledge_id);
    if (current) acceptReview(current.receipt, attemptFor(state.dashboard.current).key);
    const conflicts = body.results.filter(item => item.status === "conflict");
    if (conflicts.length) showError(conflicts[0].error.message, () => loadToday(false));
    if (saved.size) toast(`대기 답안 ${saved.size}개를 중복 없이 저장했습니다.`);
  } catch { setConnection(false, "답안 동기화 대기"); }
}

async function loadToday(useCache = true) {
  setBusy(true);
  clearError();
  try {
    let data = await api("/api/dashboard");
    writeJson(cacheKey("today"), data);
    setConnection(true);
    renderToday(data, false);
    if (await resumeTodayJob() === "finished") {
      data = await api("/api/dashboard");
      writeJson(cacheKey("today"), data);
      renderToday(data, false);
    }
    await syncOutbox();
  } catch (error) {
    const cached = useCache && readJson(cacheKey("today"));
    if (cached) {
      setConnection(false, `마지막 동기화 ${formatDate(cached.server_time)}`);
      renderToday(cached, true);
    } else {
      setConnection(false, error.status ? "서버 오류" : "");
      showError(error.message || "오늘 학습을 불러오지 못했습니다.", () => loadToday(false), true);
    }
  } finally { setBusy(false); restoreScroll("today"); }
}

function renderCurriculum(data) {
  const root = $("#curriculum-content");
  root.replaceChildren();
  if (data.status !== "ready") {
    const empty = document.createElement("section");
    empty.className = "empty";
    const title = document.createElement("h2");
    title.textContent = data.message;
    const button = document.createElement("button");
    button.className = "secondary";
    button.textContent = "경로 세우기";
    button.addEventListener("click", () => navigate("today"));
    empty.append(title, paragraph("목표, 현재 수행, 순서, 제외 범위, 증명 방법을 대화로 정합니다."), button);
    return root.append(empty);
  }
  const destination = document.createElement("section");
  destination.className = "destination";
  const title = document.createElement("h2");
  title.textContent = "최종적으로 해낼 일";
  destination.append(title, paragraph(data.destination.use_context), list(data.destination.capabilities));
  root.append(destination);
  for (const step of data.steps) {
    const section = document.createElement("section");
    section.className = "step";
    section.dataset.status = step.status;
    const number = paragraph(String(step.order).padStart(2, "0"), "step-number");
    const body = document.createElement("div");
    body.append(paragraph(
      step.status === "active" ? "지금 학습" : step.status === "completed" ? "완료" : "잠김",
      "step-status",
    ));
    const heading = document.createElement("h2");
    heading.textContent = step.title;
    body.append(heading, paragraph(step.reason));
    if (step.locked_reason) body.append(paragraph(step.locked_reason, "meta"));
    for (const milestone of step.milestones) {
      const proof = document.createElement("div");
      proof.className = "milestone";
      const h3 = document.createElement("h3");
      h3.textContent = milestone.title;
      proof.append(h3, paragraph(`증명: ${milestone.proof}`), list(milestone.criteria));
      if (milestone.evidence) proof.append(paragraph(`통과 증거: ${milestone.evidence.title} v${milestone.evidence.version}`, "next-due"));
      body.append(proof);
    }
    section.append(number, body);
    root.append(section);
  }
  if (data.cut_list.length) {
    const cut = document.createElement("section");
    cut.className = "destination";
    const heading = document.createElement("h2");
    heading.textContent = "지금은 배우지 않는 것";
    cut.append(heading);
    for (const item of data.cut_list) {
      const h3 = document.createElement("h3");
      h3.textContent = item.topic;
      cut.append(h3, paragraph(item.reason), paragraph(`다시 볼 때: ${item.reconsider_when}`, "meta"));
    }
    root.append(cut);
  }
}

async function loadCurriculum(useCache = true) {
  setBusy(true); clearError();
  try {
    const data = await api("/api/curriculum");
    state.curriculum = data; writeJson(cacheKey("curriculum"), data); setConnection(true); renderCurriculum(data);
  } catch (error) {
    const cached = useCache && readJson(cacheKey("curriculum"));
    if (cached) { setConnection(false, "저장된 경로"); renderCurriculum(cached); }
    else showError(error.message, () => loadCurriculum(false), true);
  } finally { setBusy(false); restoreScroll("curriculum"); }
}

function renderHistory(reset = true) {
  const root = $("#history-list");
  if (reset) root.replaceChildren();
  for (const event of state.history.slice(root.children.length)) {
    const item = document.createElement("li");
    const heading = document.createElement("h2");
    heading.textContent = event.title;
    const detail = paragraph(`${humanStatus(event.kind)} · ${humanStatus(event.phase)} · ${humanStatus(event.result)}`, "meta");
    const time = document.createElement("time");
    time.dateTime = event.at;
    time.textContent = formatDate(event.at);
    item.append(heading, detail, time);
    root.append(item);
  }
  $("#history-empty").hidden = state.history.length > 0;
  $("#history-more").hidden = !state.historyCursor;
}

async function loadHistory(more = false) {
  setBusy(true); clearError();
  try {
    const cursor = more ? state.historyCursor : "0";
    const data = await api(`/api/history?limit=15&cursor=${encodeURIComponent(cursor || "0")}`);
    state.history = more ? [...state.history, ...data.items] : data.items;
    state.historyCursor = data.next_cursor;
    if (!more) writeJson(cacheKey("history"), data);
    setConnection(true); renderHistory(!more);
  } catch (error) {
    const cached = !more && readJson(cacheKey("history"));
    if (cached) { state.history = cached.items; state.historyCursor = cached.next_cursor; setConnection(false, "저장된 기록"); renderHistory(); }
    else showError(error.message, () => loadHistory(more), true);
  } finally { setBusy(false); restoreScroll("history"); }
}

function buildJob(jobValue, view, record = activeJob()) {
  const job = normalizeJob(jobValue);
  const article = document.createElement("article");
  article.className = "job";
  article.dataset.state = job.state;
  article.setAttribute("aria-live", "polite");
  const heading = document.createElement(view === "today" ? "h2" : "h3");
  heading.textContent = job.input_request ? ({
    librarian_sources: "Librarian · 자료 확인",
    tutor_application: "Tutor · 적용", tutor_retrieval: "Tutor · 지연 인출",
    artifact_submission: "Editor · 결과물", artifact_revision: "Editor · 수정",
    roommate_answer: "Roommate · 다른 관점",
  })[job.input_request.kind] || "다음 한 가지" : job.status;
  article.append(heading);
  if (job.response) article.append(paragraph(job.response));

  if (job.input_request && !job.reply_consumed) {
    const prompt = document.createElement("section");
    prompt.className = "job-question";
    prompt.append(paragraph(job.input_request.prompt, "job-prompt"));
    const form = document.createElement("form");
    const draftKey = cacheKey(`job-reply:${job.id}`);
    form.innerHTML = `
      <div class="field"><label for="reply-${job.id}">내 답</label><textarea id="reply-${job.id}" required maxlength="4000"></textarea><p class="field-help">답하면 같은 학습 활동이 이어집니다.</p></div>
      <button class="primary" type="submit">답하고 계속하기</button>`;
    const answer = $("textarea", form);
    answer.value = localStorage.getItem(draftKey) || "";
    answer.addEventListener("input", () => localStorage.setItem(draftKey, answer.value));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const value = answer.value.trim();
      if (!value) return;
      const button = $("button", form);
      button.disabled = true;
      const created = await createJob(value, {view, replyTo: job.id});
      if (created) localStorage.removeItem(draftKey);
      else button.disabled = false;
    });
    prompt.append(form);
    article.append(prompt);
  }

  if (["queued", "running"].includes(job.state)) {
    article.append(paragraph("이 화면을 닫아도 진행 상태는 보존됩니다.", "meta"));
    const cancel = document.createElement("button");
    cancel.className = "secondary";
    cancel.textContent = "중단";
    cancel.addEventListener("click", () => cancelJob(job.id));
    article.append(cancel);
  } else if (["failed", "interrupted"].includes(job.state) && record?.id === job.id) {
    const actions = document.createElement("div");
    actions.className = "button-row";
    if (record.message) {
      const retry = document.createElement("button");
      retry.textContent = "다시 시도";
      retry.addEventListener("click", () => createJob(record.message, {
        view: record.view || view, replyTo: record.replyTo || null,
      }));
      actions.append(retry);
    }
    const reset = document.createElement("button");
    reset.className = "secondary";
    reset.textContent = "입력으로 돌아가기";
    reset.addEventListener("click", async () => {
      clearActiveJob(job.id);
      if (view === "today") await loadToday(false);
      else await loadCampus(false);
    });
    actions.append(reset);
    article.append(actions);
  }
  return article;
}

function renderTodayJob(job, record = activeJob()) {
  const content = $("#today-content");
  content.replaceChildren(buildJob(job, "today", record));
}

async function resumeTodayJob() {
  let record = activeJob();
  if (!record || (record.view !== "today" && record.message)) return "none";
  try {
    const {job: value} = await api(`/api/jobs/${encodeURIComponent(record.id)}?summary=1`);
    const job = normalizeJob(value);
    if (job.state === "completed" && !job.input_request) {
      clearActiveJob(job.id);
      localStorage.removeItem(cacheKey("setup-goal"));
      return "finished";
    }
    if (record.view !== "today") {
      return "none";
    }
    state.revision = job.result_revision || state.revision;
    renderTodayJob(job, record);
    scheduleJobPoll();
    return "shown";
  } catch (error) {
    if (error.status === 404) clearActiveJob(record.id);
    return "none";
  }
}

function renderCampus(data) {
  state.campus = data;
  const root = $("#campus-content");
  root.replaceChildren();
  const profile = document.createElement("section");
  profile.className = "destination";
  const heading = document.createElement("h2");
  heading.textContent = data.goal || "나의 대학을 시작하세요";
  profile.append(heading);
  if (data.level) profile.append(paragraph(`현재 수행: ${data.level}`));
  if (data.focus?.length) profile.append(list(data.focus));
  root.append(profile);

  const support = document.createElement("section");
  support.className = "workshop";
  support.innerHTML = `
    <h2>학습 지원 요청</h2>
    <p>목표 조정, 새 수업, 자료 확인, 다른 관점 중 지금 필요한 일을 한 문장으로 적으세요.</p>
    <form id="support-form"><div class="field"><label for="support-message">무엇을 이어갈까요?</label><textarea id="support-message" maxlength="4000" required></textarea></div><button class="primary" type="submit">다음 학습 준비 요청</button></form>`;
  const supportDraft = cacheKey("support-draft");
  $("#support-message", support).value = localStorage.getItem(supportDraft) || "";
  $("#support-message", support).addEventListener("input", event => localStorage.setItem(supportDraft, event.target.value));
  $("#support-form", support).addEventListener("submit", async event => {
    event.preventDefault();
    const message = $("#support-message", support).value.trim();
    if (!message) return;
    if (await createJob(message)) localStorage.removeItem(supportDraft);
  });
  root.append(support);

  if (data.jobs?.length) {
    const jobs = document.createElement("section");
    jobs.className = "workshop";
    const h2 = document.createElement("h2");
    h2.textContent = "이어지는 학습 지원";
    jobs.append(h2);
    for (const job of data.jobs) jobs.append(buildJob(job, "campus"));
    root.append(jobs);
  }

  const perspective = document.createElement("section");
  perspective.className = "workshop";
  const perspectiveTitle = document.createElement("h2");
  perspectiveTitle.textContent = "다른 분야에서 보기";
  perspective.append(perspectiveTitle);
  if (!data.perspectives.length) perspective.append(paragraph("필요할 때 현재 문제를 먼 분야의 원리와 연결해 비유의 한계까지 확인합니다."));
  for (const item of data.perspectives) {
    const details = document.createElement("details");
    details.className = "perspective";
    details.open = item.pending;
    const summary = document.createElement("summary");
    summary.textContent = `${item.outside_field} · ${item.lens}`;
    details.append(summary, paragraph(item.question));
    if (item.pending) {
      const form = document.createElement("form");
      form.innerHTML = `<div class="field"><label>내 연결 생각<textarea required maxlength="4000"></textarea></label></div><button type="submit">답하고 연결 확인하기</button>`;
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const answer = $("textarea", form).value.trim();
        if (answer) await createJob(`다른 분야 연결 질문에 대한 내 답: ${answer}. mapping과 비유가 깨지는 limits를 함께 기록해줘.`);
      });
      details.append(form);
    } else {
      details.append(paragraph(`연결: ${item.mapping || "연결 없음"}`), paragraph(`비유가 깨지는 곳: ${item.limits || "해당 없음"}`, "connection-note"));
    }
    perspective.append(details);
  }
  root.append(perspective);

  const artifacts = document.createElement("section");
  artifacts.className = "workshop";
  const artifactTitle = document.createElement("h2");
  artifactTitle.textContent = "결과물 작업실";
  artifacts.append(artifactTitle, paragraph("내 초안을 저장하면 일곱 기준의 피드백을 요청하고, 수정본은 새 버전으로 남습니다."));
  for (const item of data.artifacts) artifacts.append(renderArtifact(item));
  artifacts.append(renderArtifactForm(data.milestones));
  root.append(artifacts);
  scheduleJobPoll();
}

function renderArtifact(item) {
  const details = document.createElement("details");
  details.className = "workshop";
  const summary = document.createElement("summary");
  summary.textContent = `${item.title} · v${item.version} · ${humanStatus(item.status)}`;
  details.append(summary, paragraph(`목적: ${item.purpose} · 독자: ${item.audience}`));
  if (item.findings.length) {
    for (const finding of item.findings) {
      const node = document.createElement("div");
      node.className = "finding";
      const title = document.createElement("h3");
      title.textContent = `${finding.dimension} · ${finding.status}`;
      node.append(title, paragraph(`문제 구간: ${finding.evidence_span}`), paragraph(finding.diagnosis), paragraph(`내가 할 수정: ${finding.revision_action}`, "next-due"));
      details.append(node);
    }
  }
  if (item.status === "needs_revision") {
    const form = document.createElement("form");
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.textContent = "내 수정본";
    const textarea = document.createElement("textarea");
    textarea.required = true;
    textarea.maxLength = 10000;
    textarea.value = item.content;
    label.append(textarea);
    field.append(label);
    const button = document.createElement("button");
    button.type = "submit";
    button.textContent = "새 버전 저장하고 다시 검토받기";
    form.append(field, button);
    form.addEventListener("submit", event => { event.preventDefault(); reviseArtifact(item, textarea.value, button); });
    details.append(form);
  } else if (item.status === "draft") {
    const button = document.createElement("button");
    button.className = "secondary";
    button.textContent = "이 버전 피드백 요청";
    button.addEventListener("click", () => createJob(`내 결과물 ‘${item.title}’의 현재 학습자 버전을 검토하고, 문제 구간과 내가 할 수정 행동을 알려줘.`));
    details.append(button);
  }
  return details;
}

function renderArtifactForm(milestones) {
  const details = document.createElement("details");
  details.className = "workshop";
  const summary = document.createElement("summary");
  summary.textContent = "새 결과물 제출";
  const form = document.createElement("form");
  form.innerHTML = `
    <div class="field"><label>제목<input name="title" required maxlength="120"></label></div>
    <div class="field"><label>목적<input name="purpose" required maxlength="300"></label></div>
    <div class="field"><label>읽을 사람<input name="audience" required maxlength="300"></label></div>
    <div class="field"><label>연결할 수행 목표<select name="milestone"><option value="">독립 결과물</option></select></label></div>
    <div class="field"><label>내 초안<textarea name="content" required maxlength="10000"></textarea></label></div>
    <button type="submit">초안 저장하고 피드백 요청</button>`;
  const select = $("select", form);
  for (const milestone of milestones) {
    const option = document.createElement("option");
    option.value = milestone.id;
    option.textContent = milestone.title;
    select.append(option);
  }
  form.addEventListener("submit", event => { event.preventDefault(); saveArtifact(new FormData(form), $("button", form)); });
  details.append(summary, form);
  return details;
}

async function saveArtifact(data, button) {
  button.disabled = true;
  try {
    const result = await api("/api/artifacts", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        title: data.get("title"), purpose: data.get("purpose"), audience: data.get("audience"),
        content: data.get("content"), milestone_id: data.get("milestone") || null,
        expected_revision: state.revision,
      }),
    });
    state.revision = result.state_revision;
    toast("학습자 초안을 버전 1로 저장했습니다.");
    await createJob(`내 결과물 ‘${result.artifact.title}’의 학습자 버전을 검토하고 문제 구간과 수정 행동을 알려줘.`);
    await loadCampus(false);
  } catch (error) { showError(error.message, () => loadCampus(false)); }
  finally { button.disabled = false; }
}

async function reviseArtifact(item, content, button) {
  if (content.trim() === item.content.trim()) return toast("수정된 내용을 저장해 주세요.");
  button.disabled = true;
  try {
    const result = await api(`/api/artifacts/${encodeURIComponent(item.id)}/revision`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({content, expected_revision: state.revision}),
    });
    state.revision = result.state_revision;
    toast(`학습자 수정본 v${result.artifact.version}을 저장했습니다.`);
    await createJob(`내 결과물 ‘${item.title}’의 새 학습자 버전을 다시 검토하고 해결·회귀 finding을 기록해줘.`);
    await loadCampus(false);
  } catch (error) { showError(error.message, () => loadCampus(false)); }
  finally { button.disabled = false; }
}

async function createJob(message, options = {}) {
  const view = options.view || state.view;
  const replyTo = options.replyTo || null;
  if (!navigator.onLine) return toast("학습 지원 요청은 연결된 뒤 보낼 수 있습니다. 작성 내용은 이 기기에 남아 있습니다.");
  if (!state.revision) return toast("최신 학습 상태를 먼저 불러와 주세요.");
  try {
    const body = await api("/api/jobs", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message, expected_revision: state.revision, reply_to: replyTo}),
    });
    const record = {id: body.job.id, view, message, replyTo};
    saveActiveJob(record);
    if (view === "today") {
      renderTodayJob(body.job, record);
      scheduleJobPoll();
    } else {
      await loadCampus(false);
    }
    return body;
  } catch (error) {
    showError(error.message, () => createJob(message, options));
    return null;
  }
}

async function cancelJob(id) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    if (activeJob()?.view === "today") await resumeTodayJob();
    else await loadCampus(false);
  } catch (error) { showError(error.message, () => cancelJob(id)); }
}

function scheduleJobPoll() {
  clearTimeout(state.pollTimer);
  const fallback = state.view === "campus"
    ? state.campus?.jobs?.find(job => ["queued", "running"].includes(job.state))
    : null;
  const stored = activeJob();
  const record = stored?.view === state.view
    ? stored
    : fallback ? {id: fallback.id, view: "campus"} : null;
  if (!record) return;
  state.pollTimer = setTimeout(async () => {
    try {
      const {job: value} = await api(`/api/jobs/${encodeURIComponent(record.id)}?summary=1`);
      const job = normalizeJob(value);
      const revisionChanged = Boolean(job.result_revision && job.result_revision !== state.revision);
      state.revision = job.result_revision || state.revision;
      if (record.view === "today") {
        if (job.state === "completed" && !job.input_request) {
          clearActiveJob(job.id);
          localStorage.removeItem(cacheKey("setup-goal"));
          await loadToday(false);
        } else if (revisionChanged) {
          await loadToday(false);
        } else {
          renderTodayJob(job, record);
          if (["queued", "running"].includes(job.state)) scheduleJobPoll();
        }
      } else if (!["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
        await loadCampus(false);
      } else scheduleJobPoll();
    } catch { scheduleJobPoll(); }
  }, document.hidden ? 4000 : 1800);
}

async function loadCampus(useCache = true) {
  setBusy(true); clearError();
  try {
    const data = await api("/api/campus");
    state.revision = data.state_revision || state.revision;
    const newestRevision = data.jobs.map(job => job.result_revision).filter(Boolean).at(0);
    if (newestRevision) state.revision = newestRevision;
    writeJson(cacheKey("campus"), data); setConnection(true); renderCampus(data);
  } catch (error) {
    const cached = useCache && readJson(cacheKey("campus"));
    if (cached) { setConnection(false, "저장된 대학 정보"); renderCampus(cached); }
    else showError(error.message, () => loadCampus(false), true);
  } finally { setBusy(false); restoreScroll("campus"); }
}

function saveScroll() { localStorage.setItem(cacheKey(`scroll:${state.view}`), String(scrollY)); }
function restoreScroll(view) {
  const saved = Number(localStorage.getItem(cacheKey(`scroll:${view}`)) || 0);
  requestAnimationFrame(() => scrollTo({top: saved, behavior: "auto"}));
}
function navigate(view) {
  if (!VIEWS.includes(view)) return;
  if (state.view === view) return showView(view, false);
  saveScroll();
  location.hash = view;
}
async function showView(view, focus = true) {
  state.view = VIEWS.includes(view) ? view : "today";
  for (const node of $$(".app-view")) node.hidden = node.dataset.page !== state.view;
  for (const link of $$(".bottom-nav a")) {
    if (link.dataset.view === state.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${({today: "오늘", curriculum: "커리큘럼", history: "기록", campus: "나의 대학"})[state.view]} · become`;
  clearTimeout(state.pollTimer);
  if (state.view === "today") await loadToday(true);
  if (state.view === "curriculum") await loadCurriculum(true);
  if (state.view === "history") await loadHistory(false);
  if (state.view === "campus") await loadCampus(true);
  if (focus) $("#main").focus({preventScroll: true});
}

$("#save-token").addEventListener("click", () => {
  const value = $("#token").value.trim();
  if (!value) return toast("연결 토큰을 입력해 주세요.");
  sessionStorage.setItem("become-token", value);
  $("#token").value = "";
  showView(state.view, false);
});
$("#retry-view").addEventListener("click", () => state.retry?.());
$("#history-more").addEventListener("click", () => loadHistory(true));
document.addEventListener("click", event => {
  const view = event.target.closest("[data-view]")?.dataset.view;
  const go = event.target.closest("[data-go]")?.dataset.go;
  if (view || go) { event.preventDefault(); navigate(view || go); }
});
addEventListener("hashchange", () => showView(location.hash.slice(1), true));
addEventListener("online", async () => { setConnection(true); await syncOutbox(); await showView(state.view, false); });
addEventListener("offline", () => setConnection(false, outbox().length ? "답안 동기화 대기" : "저장된 내용"));
addEventListener("scroll", () => { clearTimeout(saveScroll.timer); saveScroll.timer = setTimeout(saveScroll, 160); }, {passive: true});
addEventListener("pagehide", () => { clearTimeout(saveTimer); saveScroll(); });
document.addEventListener("visibilitychange", scheduleJobPoll);
if (window.visualViewport) {
  const keyboard = () => document.body.classList.toggle("keyboard-open", window.visualViewport.height < innerHeight * .72);
  window.visualViewport.addEventListener("resize", keyboard);
}

async function registerWorker() {
  if (!("serviceWorker" in navigator)) return;
  const registration = await navigator.serviceWorker.register("/sw.js");
  const announce = worker => {
    if (!worker) return;
    $("#update-banner").hidden = false;
    $("#apply-update").onclick = () => worker.postMessage({type: "SKIP_WAITING"});
  };
  if (registration.waiting) announce(registration.waiting);
  registration.addEventListener("updatefound", () => {
    const worker = registration.installing;
    worker?.addEventListener("statechange", () => {
      if (worker.state === "installed" && navigator.serviceWorker.controller) announce(worker);
    });
  });
  let refreshing = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!refreshing) { refreshing = true; location.reload(); }
  });
}

registerWorker().catch(() => {});
showView(state.view, false);

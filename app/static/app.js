const API_URL = "/api/notes";
const FILES_API = "/api/files";
const MAX_LENGTH = 100000;
const MAX_NOTE_FILES = 10;
const PASSWORD_MIN_LENGTH = 4;
const PASSWORD_MAX_LENGTH = 128;

const textEl = document.getElementById("text");
const infoEl = document.getElementById("info");
const counterEl = document.getElementById("counter");
const expiryEl = document.getElementById("expiry");
const burnEl = document.getElementById("burn");
const editorAreaEl = document.getElementById("editor-area");
const burnConfirmEl = document.getElementById("burn-confirm");
const burnRevealBtn = document.getElementById("burn-reveal");
const saveBtn = document.getElementById("save");
const copyBtn = document.getElementById("copy");
const statusEl = document.getElementById("status");
const noteTitleEl = document.getElementById("note-title");
const sharePanelEl = document.getElementById("share-panel");
const shareStatusEl = document.getElementById("share-status");
const shareUrlEl = document.getElementById("share-url");
const shareCopyBtn = document.getElementById("share-copy");
const shareQrEl = document.getElementById("share-qr");
const shareQrDownload = document.getElementById("share-qr-download");
const homeViewEl = document.getElementById("home-view");
const noteViewEl = document.getElementById("note-view");
const channelViewEl = document.getElementById("channel-view");
const navNoteEl = document.getElementById("nav-note");
const navChannelEl = document.getElementById("nav-channel");
const attachBtn = document.getElementById("attach");
const attachInput = document.getElementById("attach-input");
const attachmentListEl = document.getElementById("attachment-list");
const filesCountEl = document.getElementById("files-count");
const noteFilesEl = document.getElementById("note-files");
const noteFilesHintEl = document.getElementById("note-files-hint");
const burnFilesEl = document.getElementById("burn-files");
const passwordEl = document.getElementById("note-password");
const passwordToggleBtn = document.getElementById("toggle-password");
const passwordHintEl = document.getElementById("password-hint");
const optionsEl = document.getElementById("options");
const optionsToggleBtn = document.getElementById("options-toggle");
const optionsSummaryEl = document.getElementById("options-summary");
const unlockEl = document.getElementById("unlock");
const notFoundEl = document.getElementById("not-found");
const unlockInfoEl = document.getElementById("unlock-info");
const unlockFormEl = document.getElementById("unlock-form");
const unlockPasswordEl = document.getElementById("unlock-password");
const unlockSubmitBtn = document.getElementById("unlock-submit");
const unlockErrorEl = document.getElementById("unlock-error");

let currentSlug = null;
// Заметка, которую ещё не показали: ждём подтверждения или пароля
let pendingSlug = null;
let unlockTimer = null;
let statusTimer = null;
let saving = false;
// Файлы для новой заметки: загружаются сразу при выборе, в заметку уходят их id
let attachments = [];

function slugFromPath() {
  const match = location.pathname.match(/^\/n\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function noteUrl(slug) {
  return `${location.origin}/n/${encodeURIComponent(slug)}`;
}

function showStatus(message, kind = "success") {
  clearTimeout(statusTimer);
  statusEl.textContent = message;
  statusEl.className = `status ${kind}`;
  if (kind === "success") {
    statusTimer = setTimeout(() => {
      statusEl.textContent = "";
    }, 3000);
  }
}

function updateCounter() {
  counterEl.textContent = `${textEl.value.length.toLocaleString("ru-RU")} / ${MAX_LENGTH.toLocaleString("ru-RU")}`;
}

function formatDate(iso) {
  return new Date(iso).toLocaleString("ru-RU", {
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function setTitle(slug) {
  if (!slug) {
    noteTitleEl.textContent = "Новая заметка";
    return;
  }
  const code = el("span", "editor-slug", slug);
  noteTitleEl.replaceChildren("Заметка ", code);
}

function showShareStatus(message, kind = "success") {
  shareStatusEl.textContent = message;
  shareStatusEl.className = `share-status ${kind}`;
}

function showSharePanel(slug) {
  sharePanelEl.hidden = !slug;
  showShareStatus("");
  if (!slug) {
    return;
  }
  const qrUrl = `${API_URL}/${encodeURIComponent(slug)}/qr`;
  shareUrlEl.value = noteUrl(slug);
  shareQrEl.src = qrUrl;
  shareQrDownload.href = qrUrl;
  shareQrDownload.download = `copypaste-${slug}.svg`;
}

function setNote(note) {
  currentSlug = note ? note.slug : null;
  setTitle(currentSlug);
  showSharePanel(currentSlug);

  if (!note) {
    infoEl.textContent = "";
    return;
  }
  const parts = [];
  if (note.has_password) {
    parts.push("Защищена паролем");
  }
  if (note.burn_after_read) {
    parts.push("Удалится после первого прочтения");
  }
  parts.push(note.expires_at ? `Удалится ${formatDate(note.expires_at)}` : "Хранится бессрочно");
  infoEl.textContent = parts.join(" · ");
}

function showScreen(name) {
  editorAreaEl.hidden = name !== "editor";
  burnConfirmEl.hidden = name !== "burn";
  unlockEl.hidden = name !== "unlock";
  notFoundEl.hidden = name !== "missing";
}

function passwordLength(value) {
  // Сервер считает символы, а не UTF-16: эмодзи — один символ, а не два
  return [...value].length;
}

function resetPasswordField() {
  passwordEl.value = "";
  passwordEl.type = "password";
  passwordToggleBtn.textContent = "Показать";
  passwordHintEl.hidden = true;
  updateOptionsSummary();
}

// На телефоне параметры свёрнуты в строку: по ней видно, что выбрано, не раскрывая
function updateOptionsSummary() {
  const parts = [expiryEl.querySelector("input:checked").nextElementSibling.textContent];
  if (burnEl.checked) {
    parts.push("одноразовая");
  }
  if (passwordEl.value) {
    parts.push("с паролем");
  }
  optionsSummaryEl.textContent = parts.join(" · ");
}

function setOptionsExpanded(expanded) {
  optionsEl.classList.toggle("expanded", expanded);
  optionsToggleBtn.setAttribute("aria-expanded", String(expanded));
}

function togglePasswordVisibility() {
  const show = passwordEl.type === "password";
  passwordEl.type = show ? "text" : "password";
  passwordToggleBtn.textContent = show ? "Скрыть" : "Показать";
}

function updateSaveButton() {
  const uploading = attachments.some((upload) => upload.state === "uploading");
  saveBtn.disabled = saving || uploading;
  if (saving) {
    saveBtn.textContent = "Сохраняю…";
  } else {
    saveBtn.textContent = uploading ? "Загружаются файлы…" : "Сохранить";
  }
}

function setBusy(busy) {
  saving = busy;
  updateSaveButton();
}

function updateFilesCount() {
  filesCountEl.textContent = attachments.length
    ? `${attachments.length} из ${MAX_NOTE_FILES}`
    : "";
}

function addAttachments(files) {
  const room = MAX_NOTE_FILES - attachments.length;
  if (room <= 0) {
    showStatus(`В заметке не больше ${MAX_NOTE_FILES} файлов`, "error");
    return;
  }
  if (files.length > room) {
    showStatus(`Добавлены первые ${room}: в заметке не больше ${MAX_NOTE_FILES} файлов`, "error");
  }
  for (const file of files.slice(0, room)) {
    const upload = startUpload(file, {
      url: () => FILES_API,
      list: attachmentListEl,
      onChange: updateSaveButton,
      onRemoved: (removed) => {
        attachments = attachments.filter((item) => item !== removed);
        updateSaveButton();
        updateFilesCount();
      },
    });
    attachments.push(upload);
  }
  updateSaveButton();
  updateFilesCount();
}

function clearAttachments() {
  for (const upload of attachments) {
    upload.state = "removed";
    upload.request?.abort();
  }
  attachments = [];
  attachmentListEl.replaceChildren();
  updateSaveButton();
  updateFilesCount();
}

function clearNoteFiles() {
  noteFilesEl.replaceChildren();
  noteFilesEl.hidden = true;
  noteFilesHintEl.hidden = true;
}

function renderNoteFiles(files, hint = "") {
  noteFilesEl.replaceChildren(
    ...files.map((file) => {
      const item = el("li");
      item.append(createFileCard(file).el);
      return item;
    }),
  );
  noteFilesEl.hidden = files.length === 0;
  noteFilesHintEl.textContent = hint;
  noteFilesHintEl.hidden = !hint || files.length === 0;
}

async function errorMessage(response) {
  if (response.status === 404) {
    return "Заметка не найдена или срок её жизни истёк";
  }
  if (response.status === 422) {
    try {
      const body = await response.json();
      if (typeof body.detail === "string") {
        // Файл привязать не удалось: загружен слишком давно или уже в другой заметке
        return "Часть файлов недоступна — уберите их и загрузите заново";
      }
      const msg = body.detail?.[0]?.msg;
      if (msg) {
        return `Некорректные данные: ${msg}`;
      }
    } catch {
      // тело не JSON — покажем общее сообщение
    }
    return "Некорректные данные";
  }
  return `Ошибка сервера (${response.status})`;
}

function burnFilesText(count) {
  return count
    ? `В заметке ${count} ${plural(count, "файл", "файла", "файлов")}: ` +
        "после открытия каждый можно скачать один раз в течение 10 минут."
    : "";
}

// warn — заморозка: пароль тут ни при чём, поэтому без красной рамки у поля
function showUnlockError(message, kind = "error") {
  unlockErrorEl.textContent = message;
  unlockErrorEl.className = `unlock-error ${kind}`;
  unlockErrorEl.hidden = !message;
  if (message && kind === "error") {
    unlockPasswordEl.setAttribute("aria-invalid", "true");
  } else {
    unlockPasswordEl.removeAttribute("aria-invalid");
  }
}

function stopUnlockCountdown() {
  clearInterval(unlockTimer);
  unlockTimer = null;
  unlockSubmitBtn.disabled = false;
  unlockSubmitBtn.textContent = "Открыть";
  unlockPasswordEl.disabled = false;
}

function startUnlockCountdown(seconds) {
  stopUnlockCountdown();
  const until = Date.now() + seconds * 1000;
  const tick = () => {
    const left = Math.ceil((until - Date.now()) / 1000);
    if (left <= 0) {
      stopUnlockCountdown();
      showUnlockError("");
      unlockPasswordEl.focus();
      return;
    }
    const wait = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    unlockSubmitBtn.textContent = `Открыть через ${wait}`;
  };
  unlockSubmitBtn.disabled = true;
  unlockPasswordEl.value = "";
  unlockPasswordEl.disabled = true;
  showUnlockError("Слишком много неверных попыток", "warn");
  tick();
  unlockTimer = setInterval(tick, 1000);
}

function showUnlock(note) {
  const count = note.files.length;
  let info = "";
  if (note.burn_after_read) {
    info = "После открытия заметка удалится с сервера — открыть её можно только один раз. " +
      burnFilesText(count);
  } else if (count) {
    info = `В заметке ${count} ${plural(count, "файл", "файла", "файлов")} — они откроются вместе с текстом.`;
  }
  unlockInfoEl.textContent = info.trim();
  unlockInfoEl.hidden = !info;
  unlockPasswordEl.value = "";
  showUnlockError("");
  showScreen("unlock");
  unlockPasswordEl.focus();
}

async function unlockFailure(response) {
  switch (response.status) {
    case 403: {
      const body = await response.json().catch(() => null);
      const left = body?.detail?.attempts_left;
      if (typeof left !== "number") {
        return "Неверный пароль";
      }
      if (left === 0) {
        return "Неверный пароль. Попытки закончились — ввод пароля временно заморожен";
      }
      return `Неверный пароль. Осталось ${left} ${plural(left, "попытка", "попытки", "попыток")}`;
    }
    case 422:
      return `Пароль — от ${PASSWORD_MIN_LENGTH} до ${PASSWORD_MAX_LENGTH} символов`;
    case 503:
      return "Проверка пароля временно недоступна — попробуйте через минуту";
    default:
      return `Ошибка сервера (${response.status})`;
  }
}

async function unlockNote(event) {
  event.preventDefault();
  const slug = pendingSlug;
  const password = unlockPasswordEl.value;
  if (!slug || unlockSubmitBtn.disabled) {
    return;
  }
  const length = passwordLength(password);
  if (length < PASSWORD_MIN_LENGTH || length > PASSWORD_MAX_LENGTH) {
    showUnlockError(`Пароль — от ${PASSWORD_MIN_LENGTH} до ${PASSWORD_MAX_LENGTH} символов`);
    return;
  }

  unlockSubmitBtn.disabled = true;
  unlockSubmitBtn.textContent = "Проверяю…";
  showUnlockError("");
  try {
    const response = await fetch(`${API_URL}/${encodeURIComponent(slug)}/unlock`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    // Пока ждали ответ, пользователь мог уйти на другую страницу
    if (slug !== pendingSlug) {
      return;
    }
    if (response.ok) {
      const note = await response.json();
      pendingSlug = null;
      unlockPasswordEl.value = "";
      showScreen("editor");
      showNote(note);
      return;
    }
    if (response.status === 404) {
      pendingSlug = null;
      showScreen("missing");
      return;
    }
    // Retry-After приходит с 429 и с той неверной попыткой (403), что включила заморозку
    const retryAfter = Number(response.headers.get("Retry-After"));
    if ((response.status === 429 || response.status === 403) && retryAfter > 0) {
      startUnlockCountdown(retryAfter);
      return;
    }
    if (response.status === 429) {
      showUnlockError("Слишком много неверных попыток. Попробуйте позже", "warn");
      return;
    }
    showUnlockError(await unlockFailure(response));
    unlockPasswordEl.select();
  } catch {
    showUnlockError("Не удалось связаться с сервером");
  } finally {
    // Во время заморозки кнопкой управляет отсчёт
    if (!unlockTimer) {
      unlockSubmitBtn.textContent = "Открыть";
      unlockSubmitBtn.disabled = false;
    }
  }
}

async function loadNote(slug) {
  setNote(null);
  pendingSlug = null;
  stopUnlockCountdown();
  showScreen("editor");
  textEl.value = "";
  updateCounter();
  statusEl.textContent = "";
  clearAttachments();
  clearNoteFiles();

  try {
    // GET ничего не сжигает: у одноразовой заметки text придёт как null
    const response = await fetch(`${API_URL}/${encodeURIComponent(slug)}`);
    if (response.status === 404) {
      showScreen("missing");
      return;
    }
    if (!response.ok) {
      showStatus(await errorMessage(response), "error");
      return;
    }
    const note = await response.json();
    // Текст и ссылки на файлы такой заметки придут только в ответ на верный пароль
    if (note.has_password) {
      pendingSlug = slug;
      showUnlock(note);
      return;
    }
    if (note.burn_after_read) {
      pendingSlug = slug;
      burnFilesEl.textContent = burnFilesText(note.files.length);
      burnFilesEl.hidden = note.files.length === 0;
      showScreen("burn");
      return;
    }
    showNote(note);
  } catch {
    showStatus("Не удалось связаться с сервером", "error");
  }
}

function showNote(note) {
  textEl.value = note.text;
  updateCounter();
  if (note.burn_after_read) {
    // Заметки на сервере больше нет — делиться нечем
    setNote(null);
    noteTitleEl.textContent = "Заметка";
    infoEl.textContent = "Заметка удалена с сервера — это был единственный просмотр";
    renderNoteFiles(
      note.files,
      "Каждый файл можно скачать один раз в течение 10 минут — после этого он удалится.",
    );
  } else {
    setNote(note);
    renderNoteFiles(note.files);
  }
}

async function revealNote() {
  const slug = pendingSlug;
  pendingSlug = null;
  showScreen("editor");
  try {
    const response = await fetch(`${API_URL}/${encodeURIComponent(slug)}/read`, {
      method: "POST",
    });
    // Её успел прочитать кто-то другой
    if (response.status === 404) {
      showScreen("missing");
      return;
    }
    if (!response.ok) {
      showStatus(await errorMessage(response), "error");
      return;
    }
    showNote(await response.json());
  } catch {
    showStatus("Не удалось связаться с сервером", "error");
  }
}

async function saveNote() {
  // Кнопка выключена, пока идёт сохранение или грузятся файлы. Клавиши обходят кнопку:
  // без проверки они сохранили бы заметку без недогруженных файлов или вторую копию
  if (saveBtn.disabled) {
    return;
  }
  if (attachments.some((upload) => upload.state === "error")) {
    showStatus("Уберите файлы с ошибкой или загрузите их снова", "error");
    return;
  }
  const text = textEl.value;
  const fileIds = attachments
    .filter((upload) => upload.state === "done")
    .map((upload) => upload.id);
  if (!text.trim() && fileIds.length === 0) {
    showStatus("Нечего сохранять — нет ни текста, ни файлов", "error");
    return;
  }

  const body = { text, file_ids: fileIds, burn_after_read: burnEl.checked };
  const seconds = Number(expiryEl.querySelector("input:checked").value);
  if (seconds > 0) {
    body.expires_at = new Date(Date.now() + seconds * 1000).toISOString();
  }
  const password = passwordEl.value;
  if (password) {
    const length = passwordLength(password);
    if (length < PASSWORD_MIN_LENGTH || length > PASSWORD_MAX_LENGTH) {
      showStatus(`Пароль — от ${PASSWORD_MIN_LENGTH} до ${PASSWORD_MAX_LENGTH} символов`, "error");
      // В свёрнутых параметрах поле не видно — фокус ушёл бы в никуда
      setOptionsExpanded(true);
      passwordEl.focus();
      return;
    }
    body.password = password;
  }

  setBusy(true);
  try {
    const response = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      showStatus(await errorMessage(response), "error");
      return;
    }
    const note = await response.json();
    history.pushState(null, "", `/n/${encodeURIComponent(note.slug)}`);
    setNote(note);
    clearAttachments();
    resetPasswordField();
    renderNoteFiles(
      note.files,
      note.has_password ? "Скачать файлы можно будет после ввода пароля." : "",
    );
    statusEl.textContent = "";
    try {
      await copyToClipboard(noteUrl(note.slug));
      showShareStatus("Сохранено, ссылка скопирована");
    } catch {
      showShareStatus("Сохранено");
    }
  } catch {
    showStatus("Не удалось связаться с сервером", "error");
  } finally {
    setBusy(false);
  }
}

async function copyToClipboard(value) {
  // navigator.clipboard есть только в безопасном контексте (https или localhost)
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const helper = document.createElement("textarea");
  helper.value = value;
  helper.setAttribute("readonly", "");
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.appendChild(helper);
  helper.select();
  const ok = document.execCommand("copy");
  helper.remove();
  if (!ok) {
    throw new Error("copy failed");
  }
}

async function copyText() {
  if (!textEl.value) {
    showStatus("Нечего копировать — поле пустое", "error");
    return;
  }
  try {
    await copyToClipboard(textEl.value);
    showStatus("Текст скопирован");
  } catch {
    showStatus("Не удалось скопировать", "error");
  }
}

async function shareLink() {
  try {
    await copyToClipboard(shareUrlEl.value);
    showShareStatus("Ссылка скопирована");
  } catch {
    shareUrlEl.select();
    showShareStatus("Не удалось скопировать — ссылка выделена, скопируйте её вручную", "error");
  }
}

function markCurrent(navEl, current) {
  if (current) {
    navEl.setAttribute("aria-current", "page");
  } else {
    navEl.removeAttribute("aria-current");
  }
}

function showView(name) {
  homeViewEl.hidden = name !== "home";
  noteViewEl.hidden = name !== "note";
  channelViewEl.hidden = name !== "channel";
  markCurrent(navNoteEl, name === "note");
  markCurrent(navChannelEl, name === "channel");
}

function route() {
  statusEl.textContent = "";
  const channelSlug = channelSlugFromPath();
  if (channelSlug) {
    showView("channel");
    openChannel(channelSlug);
    return;
  }
  leaveChannel();

  const slug = slugFromPath();
  if (slug) {
    showView("note");
    loadNote(slug);
  } else if (location.pathname === "/note") {
    showView("note");
    pendingSlug = null;
    stopUnlockCountdown();
    showScreen("editor");
    textEl.value = "";
    updateCounter();
    setNote(null);
    clearAttachments();
    clearNoteFiles();
    resetPasswordField();
    textEl.focus();
  } else {
    showView("home");
  }
}

textEl.addEventListener("input", updateCounter);
saveBtn.addEventListener("click", saveNote);
copyBtn.addEventListener("click", copyText);
shareCopyBtn.addEventListener("click", shareLink);
burnRevealBtn.addEventListener("click", revealNote);
unlockFormEl.addEventListener("submit", unlockNote);
passwordToggleBtn.addEventListener("click", togglePasswordVisibility);
passwordEl.addEventListener("input", () => {
  passwordHintEl.hidden = !passwordEl.value;
  updateOptionsSummary();
});
expiryEl.addEventListener("change", updateOptionsSummary);
burnEl.addEventListener("change", updateOptionsSummary);
optionsToggleBtn.addEventListener("click", () => {
  setOptionsExpanded(!optionsEl.classList.contains("expanded"));
});
passwordEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    saveNote();
  }
});
bindFilePicker(attachBtn, attachInput, addAttachments);
acceptDroppedAndPasted(editorAreaEl, textEl, addAttachments);

// Новая заметка уже открыта: переход по ссылке перезагрузил бы страницу и стёр набранное
navNoteEl.addEventListener("click", (event) => {
  if (location.pathname === "/note") {
    event.preventDefault();
    textEl.focus();
  }
});

textEl.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    saveNote();
  }
});

// Ctrl+S — по физической клавише: на русской раскладке event.key здесь «ы»
document.addEventListener("keydown", (event) => {
  const editing = !noteViewEl.hidden && !editorAreaEl.hidden;
  if (editing && (event.ctrlKey || event.metaKey) && event.code === "KeyS") {
    event.preventDefault();
    saveNote();
  }
});

// Кнопки «назад/вперёд» в браузере меняют адрес без перезагрузки страницы
window.addEventListener("popstate", route);

// При перезагрузке браузер может вернуть отметки в полях — итог должен им соответствовать
updateOptionsSummary();
route();

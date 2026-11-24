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
const aboutViewEl = document.getElementById("about-view");
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

// message — русский исходник или msg(): строка перерисуется при смене языка
function showStatus(message, kind = "success") {
  clearTimeout(statusTimer);
  setText(statusEl, message);
  statusEl.className = `status ${kind}`;
  if (kind === "success") {
    statusTimer = setTimeout(() => {
      setText(statusEl, "");
    }, 3000);
  }
}

function updateCounter() {
  setText(counterEl, "{count} / {max}", { count: textEl.value.length, max: MAX_LENGTH });
}

function formatDate(iso) {
  return new Date(iso).toLocaleString(currentLocale(), {
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function setTitle(slug) {
  if (!slug) {
    setText(noteTitleEl, "Новая заметка");
    return;
  }
  setPlainText(noteTitleEl, "");
  noteTitleEl.append(el("span", null, msg("Заметка")), " ", el("span", "editor-slug", slug));
}

function showShareStatus(message, kind = "success") {
  setText(shareStatusEl, message);
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
    setText(infoEl, "");
    return;
  }
  const parts = [];
  if (note.has_password) {
    parts.push(msg("Защищена паролем"));
  }
  if (note.burn_after_read) {
    parts.push(msg("Удалится после прочтения"));
  }
  parts.push(
    note.expires_at
      ? msg("Хранится до {date}", { date: () => formatDate(note.expires_at) })
      : msg("Хранится бессрочно"),
  );
  setText(infoEl, () => parts.map((part) => part()).join(" · "));
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
  setText(passwordToggleBtn, "Показать");
  passwordHintEl.hidden = true;
  updateOptionsSummary();
}

// На телефоне параметры свёрнуты в строку: по ней видно, что выбрано, не раскрывая
function updateOptionsSummary() {
  const burn = burnEl.checked;
  const password = Boolean(passwordEl.value);
  setText(optionsSummaryEl, () => {
    const parts = [expiryEl.querySelector("input:checked").nextElementSibling.textContent];
    if (burn) {
      parts.push(t("одноразовая"));
    }
    if (password) {
      parts.push(t("с паролем"));
    }
    return parts.join(" · ");
  });
}

function setOptionsExpanded(expanded) {
  optionsEl.classList.toggle("expanded", expanded);
  optionsToggleBtn.setAttribute("aria-expanded", String(expanded));
}

function togglePasswordVisibility() {
  const show = passwordEl.type === "password";
  passwordEl.type = show ? "text" : "password";
  setText(passwordToggleBtn, show ? "Скрыть" : "Показать");
}

function updateSaveButton() {
  const uploading = attachments.some((upload) => upload.state === "uploading");
  saveBtn.disabled = saving || uploading;
  if (saving) {
    setText(saveBtn, "Сохраняю…");
  } else {
    setText(saveBtn, uploading ? "Загружаются файлы…" : "Сохранить");
  }
}

function setBusy(busy) {
  saving = busy;
  updateSaveButton();
}

function updateFilesCount() {
  if (attachments.length) {
    setText(filesCountEl, "{count} из {max}", { count: attachments.length, max: MAX_NOTE_FILES });
  } else {
    setText(filesCountEl, "");
  }
}

function addAttachments(files) {
  const room = MAX_NOTE_FILES - attachments.length;
  if (room <= 0) {
    showStatus(msg("В заметке не больше {max} файлов", { max: MAX_NOTE_FILES }), "error");
    return;
  }
  if (files.length > room) {
    showStatus(
      msg("Добавлены первые {room}: в заметке не больше {max} файлов", { room, max: MAX_NOTE_FILES }),
      "error",
    );
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
  setText(noteFilesHintEl, hint);
  noteFilesHintEl.hidden = !hint || files.length === 0;
}

// Ответ сервера с ошибкой → текст для строки статуса (msg)
async function errorMessage(response) {
  if (response.status === 404) {
    return msg("Заметка не найдена или срок её жизни истёк");
  }
  if (response.status === 422) {
    try {
      const body = await response.json();
      if (typeof body.detail === "string") {
        // Файл привязать не удалось: загружен слишком давно или уже в другой заметке
        return msg("Часть файлов устарела. Уберите их и загрузите заново");
      }
      const detail = body.detail?.[0]?.msg;
      if (detail) {
        return msg("Некорректные данные: {detail}", { detail });
      }
    } catch {
      // тело не JSON — покажем общее сообщение
    }
    return msg("Некорректные данные");
  }
  return msg("Ошибка сервера ({status})", { status: String(response.status) });
}

function burnFilesText(count) {
  if (!count) {
    return "";
  }
  if (count === 1) {
    return msg("В заметке 1 файл. После открытия его можно скачать один раз в течение 10 минут.");
  }
  return msg(
    "В заметке {count} {count:файл|файла|файлов}. После открытия каждый можно скачать один раз в течение 10 минут.",
    { count },
  );
}

// warn — заморозка: пароль тут ни при чём, поэтому без красной рамки у поля
function showUnlockError(message, kind = "error") {
  setText(unlockErrorEl, message);
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
  setText(unlockSubmitBtn, "Открыть");
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
    setText(unlockSubmitBtn, "Открыть через {wait}", { wait });
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
  const parts = [];
  if (note.burn_after_read) {
    parts.push(msg("Заметку можно открыть только один раз, потом она удалится."));
    if (count) {
      parts.push(burnFilesText(count));
    }
  } else if (count === 1) {
    parts.push(msg("В заметке 1 файл. Он откроется вместе с текстом."));
  } else if (count) {
    parts.push(msg("В заметке {count} {count:файл|файла|файлов}. Они откроются вместе с текстом.", { count }));
  }
  setText(unlockInfoEl, () => parts.map((part) => part()).join(" "));
  unlockInfoEl.hidden = parts.length === 0;
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
        return msg("Неверный пароль");
      }
      if (left === 0) {
        return msg("Неверный пароль. Попытки закончились, попробуйте позже");
      }
      return msg("Неверный пароль. Осталось {left} {left:попытка|попытки|попыток}", { left });
    }
    case 422:
      return passwordLengthText();
    case 503:
      return msg("Проверка пароля временно недоступна. Попробуйте через минуту");
    default:
      return msg("Ошибка сервера ({status})", { status: String(response.status) });
  }
}

function passwordLengthText() {
  return msg("Пароль: от {min} до {max} символов", {
    min: PASSWORD_MIN_LENGTH,
    max: PASSWORD_MAX_LENGTH,
  });
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
    showUnlockError(passwordLengthText());
    return;
  }

  unlockSubmitBtn.disabled = true;
  setText(unlockSubmitBtn, "Проверяю…");
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
    showUnlockError("Нет связи с сервером");
  } finally {
    // Во время заморозки кнопкой управляет отсчёт
    if (!unlockTimer) {
      setText(unlockSubmitBtn, "Открыть");
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
  setText(statusEl, "");
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
      setText(burnFilesEl, burnFilesText(note.files.length));
      burnFilesEl.hidden = note.files.length === 0;
      showScreen("burn");
      return;
    }
    showNote(note);
  } catch {
    showStatus("Нет связи с сервером", "error");
  }
}

function showNote(note) {
  textEl.value = note.text;
  updateCounter();
  if (note.burn_after_read) {
    // Заметки на сервере больше нет — делиться нечем
    setNote(null);
    setText(noteTitleEl, "Заметка");
    setText(infoEl, "Заметка удалена с сервера. Это был единственный просмотр");
    renderNoteFiles(
      note.files,
      "Каждый файл можно скачать один раз в течение 10 минут.",
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
    showStatus("Нет связи с сервером", "error");
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
    showStatus("Добавьте текст или файл", "error");
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
      showStatus(passwordLengthText(), "error");
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
    setText(statusEl, "");
    try {
      await copyToClipboard(noteUrl(note.slug));
      showShareStatus("Сохранено, ссылка скопирована");
    } catch {
      showShareStatus("Сохранено");
    }
  } catch {
    showStatus("Нет связи с сервером", "error");
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
    showStatus("Нечего копировать: поле пустое", "error");
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
    showShareStatus("Не удалось скопировать. Ссылка выделена, скопируйте её вручную", "error");
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
  aboutViewEl.hidden = name !== "about";
  markCurrent(navNoteEl, name === "note");
  markCurrent(navChannelEl, name === "channel");
}

function route() {
  setText(statusEl, "");
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
  } else if (location.pathname === "/about") {
    showView("about");
    // При загрузке раздел был скрыт, и браузер не докрутил до якоря — докручиваем сами
    if (location.hash) {
      document.getElementById(location.hash.slice(1))?.scrollIntoView();
    }
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

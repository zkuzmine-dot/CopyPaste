const API_URL = "/api/notes";
const FILES_API = "/api/files";
const MAX_LENGTH = 100000;
const MAX_NOTE_FILES = 10;

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
const shareBtn = document.getElementById("share");
const statusEl = document.getElementById("status");
const shareDialog = document.getElementById("share-dialog");
const shareUrlEl = document.getElementById("share-url");
const shareCopyBtn = document.getElementById("share-copy");
const shareQrEl = document.getElementById("share-qr");
const shareQrDownload = document.getElementById("share-qr-download");
const homeViewEl = document.getElementById("home-view");
const noteViewEl = document.getElementById("note-view");
const channelViewEl = document.getElementById("channel-view");
const attachBtn = document.getElementById("attach");
const attachInput = document.getElementById("attach-input");
const attachmentListEl = document.getElementById("attachment-list");
const noteFilesEl = document.getElementById("note-files");
const noteFilesHintEl = document.getElementById("note-files-hint");
const burnFilesEl = document.getElementById("burn-files");

let currentSlug = null;
// Одноразовая заметка, которую ещё не показали: ждём подтверждения
let pendingSlug = null;
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

function setNote(note) {
  currentSlug = note ? note.slug : null;
  shareBtn.disabled = !currentSlug;

  if (!note) {
    infoEl.textContent = "";
    return;
  }
  const parts = [];
  if (note.burn_after_read) {
    parts.push("Удалится после первого прочтения");
  }
  parts.push(note.expires_at ? `Удалится ${formatDate(note.expires_at)}` : "Хранится бессрочно");
  infoEl.textContent = parts.join(" · ");
}

function showEditor(visible) {
  editorAreaEl.hidden = !visible;
  burnConfirmEl.hidden = visible;
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
      },
    });
    attachments.push(upload);
  }
  updateSaveButton();
}

function clearAttachments() {
  for (const upload of attachments) {
    upload.state = "removed";
    upload.request?.abort();
  }
  attachments = [];
  attachmentListEl.replaceChildren();
  updateSaveButton();
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

async function loadNote(slug) {
  setNote(null);
  pendingSlug = null;
  showEditor(true);
  textEl.value = "";
  updateCounter();
  statusEl.textContent = "";
  clearAttachments();
  clearNoteFiles();

  try {
    // GET ничего не сжигает: у одноразовой заметки text придёт как null
    const response = await fetch(`${API_URL}/${encodeURIComponent(slug)}`);
    if (!response.ok) {
      showStatus(await errorMessage(response), "error");
      return;
    }
    const note = await response.json();
    if (note.burn_after_read) {
      pendingSlug = slug;
      const count = note.files.length;
      burnFilesEl.textContent = count
        ? `В заметке ${count} ${plural(count, "файл", "файла", "файлов")}: ` +
          "после открытия каждый можно скачать один раз в течение 10 минут."
        : "";
      burnFilesEl.hidden = count === 0;
      showEditor(false);
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
  showEditor(true);
  try {
    const response = await fetch(`${API_URL}/${encodeURIComponent(slug)}/read`, {
      method: "POST",
    });
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
  const seconds = Number(expiryEl.value);
  if (seconds > 0) {
    body.expires_at = new Date(Date.now() + seconds * 1000).toISOString();
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
    renderNoteFiles(note.files);
    showStatus("Сохранено");
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

function openShareDialog() {
  if (!currentSlug) {
    return;
  }
  const qrUrl = `${API_URL}/${encodeURIComponent(currentSlug)}/qr`;
  shareUrlEl.value = noteUrl(currentSlug);
  shareQrEl.src = qrUrl;
  shareQrDownload.href = qrUrl;
  shareQrDownload.download = `copypaste-${currentSlug}.svg`;
  shareDialog.showModal();
  shareUrlEl.select();
}

async function shareLink() {
  try {
    await copyToClipboard(shareUrlEl.value);
    shareDialog.close();
    showStatus("Ссылка скопирована");
  } catch {
    showStatus("Не удалось скопировать ссылку", "error");
  }
}

function showView(name) {
  homeViewEl.hidden = name !== "home";
  noteViewEl.hidden = name !== "note";
  channelViewEl.hidden = name !== "channel";
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
    showEditor(true);
    textEl.value = "";
    updateCounter();
    setNote(null);
    clearAttachments();
    clearNoteFiles();
    textEl.focus();
  } else {
    showView("home");
  }
}

textEl.addEventListener("input", updateCounter);
saveBtn.addEventListener("click", saveNote);
copyBtn.addEventListener("click", copyText);
shareBtn.addEventListener("click", openShareDialog);
shareCopyBtn.addEventListener("click", shareLink);
burnRevealBtn.addEventListener("click", revealNote);
bindFilePicker(attachBtn, attachInput, addAttachments);
acceptDroppedAndPasted(editorAreaEl, textEl, addAttachments);

// Клик по затемнённому фону вокруг окна закрывает его
shareDialog.addEventListener("click", (event) => {
  const rect = shareDialog.getBoundingClientRect();
  const outside =
    event.clientX < rect.left ||
    event.clientX > rect.right ||
    event.clientY < rect.top ||
    event.clientY > rect.bottom;
  if (outside) {
    shareDialog.close();
  }
});

textEl.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    saveNote();
  }
});

// Кнопки «назад/вперёд» в браузере меняют адрес без перезагрузки страницы
window.addEventListener("popstate", route);

route();

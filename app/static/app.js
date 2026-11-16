const API_URL = "/api/notes";
const MAX_LENGTH = 100000;

const textEl = document.getElementById("text");
const infoEl = document.getElementById("info");
const counterEl = document.getElementById("counter");
const expiryEl = document.getElementById("expiry");
const saveBtn = document.getElementById("save");
const copyBtn = document.getElementById("copy");
const shareBtn = document.getElementById("share");
const statusEl = document.getElementById("status");

let currentSlug = null;
let statusTimer = null;

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
  infoEl.textContent = note.expires_at
    ? `Удалится ${formatDate(note.expires_at)}`
    : "Хранится бессрочно";
}

function setBusy(busy) {
  saveBtn.disabled = busy;
  saveBtn.textContent = busy ? "Сохраняю…" : "Сохранить";
}

async function errorMessage(response) {
  if (response.status === 404) {
    return "Заметка не найдена или срок её жизни истёк";
  }
  if (response.status === 422) {
    try {
      const body = await response.json();
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
  textEl.value = "";
  updateCounter();

  try {
    const response = await fetch(`${API_URL}/${encodeURIComponent(slug)}`);
    if (!response.ok) {
      showStatus(await errorMessage(response), "error");
      return;
    }
    const note = await response.json();
    textEl.value = note.text;
    updateCounter();
    setNote(note);
    statusEl.textContent = "";
  } catch {
    showStatus("Не удалось связаться с сервером", "error");
  }
}

async function saveNote() {
  const text = textEl.value;
  if (!text.trim()) {
    showStatus("Нечего сохранять — поле пустое", "error");
    return;
  }

  const body = { text };
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

async function shareLink() {
  if (!currentSlug) {
    return;
  }
  try {
    await copyToClipboard(noteUrl(currentSlug));
    showStatus("Ссылка скопирована");
  } catch {
    showStatus("Не удалось скопировать ссылку", "error");
  }
}

function route() {
  const slug = slugFromPath();
  if (slug) {
    loadNote(slug);
  } else {
    textEl.value = "";
    updateCounter();
    setNote(null);
    statusEl.textContent = "";
  }
}

textEl.addEventListener("input", updateCounter);
saveBtn.addEventListener("click", saveNote);
copyBtn.addEventListener("click", copyText);
shareBtn.addEventListener("click", shareLink);

textEl.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    saveNote();
  }
});

// Кнопки «назад/вперёд» в браузере меняют адрес без перезагрузки страницы
window.addEventListener("popstate", route);

route();

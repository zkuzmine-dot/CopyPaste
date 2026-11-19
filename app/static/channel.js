// Канал: обмен текстом и файлами между двумя устройствами через WebSocket.
// Использует showStatus и copyToClipboard из app.js и функции файлов из files.js —
// они нужны только во время работы.

const CHANNEL_API = "/api/channels";
const CHANNEL_MAX_LENGTH = 100000;
const RECONNECT_MAX_DELAY = 15000;
const CLOSE_MESSAGE_TOO_BIG = 1009;
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_NOT_FOUND = 4404;
const CLOSE_REPLACED = 4409;
const CLOSE_CHANNEL_CLOSED = 4410;
const SLUG_PATTERN = /^[23456789abcdefghjkmnpqrstuvwxyz]{8}$/;

const chooseChannelBtn = document.getElementById("choose-channel");
const channelRoleEl = document.getElementById("channel-role");
const channelConnEl = document.getElementById("channel-conn");
const becomeSenderBtn = document.getElementById("become-sender");
const channelInviteEl = document.getElementById("channel-invite");
const channelQrEl = document.getElementById("channel-qr");
const channelUrlEl = document.getElementById("channel-url");
const channelUrlCopyBtn = document.getElementById("channel-url-copy");
const channelCodeEl = document.getElementById("channel-code");
const joinFormEl = document.getElementById("join-form");
const joinCodeEl = document.getElementById("join-code");
const channelComposeEl = document.getElementById("channel-compose");
const channelTextEl = document.getElementById("channel-text");
const channelCounterEl = document.getElementById("channel-counter");
const channelSendBtn = document.getElementById("channel-send");
const channelAttachBtn = document.getElementById("channel-attach");
const channelAttachInput = document.getElementById("channel-attach-input");
const channelUploadsEl = document.getElementById("channel-uploads");
const feedEl = document.getElementById("feed");
const feedEmptyEl = document.getElementById("feed-empty");
const channelClosedEl = document.getElementById("channel-closed");
const closedTitleEl = document.getElementById("closed-title");
const closedTextEl = document.getElementById("closed-text");
const closedActionBtn = document.getElementById("closed-action");

const channel = {
  slug: null,
  token: null,
  ws: null,
  role: null,
  peerJoined: false,
  peerOnline: false,
  connection: "",
  // id → { data, el }: одно сообщение может прийти и в state, и отдельным событием
  messages: new Map(),
  // Файл сначала загружается по HTTP, потом его id уходит в канал событием send_file
  uploads: [],
  retries: 0,
  retryTimer: null,
  tickTimer: null,
  closedAction: null,
};

function channelSlugFromPath() {
  const match = location.pathname.match(/^\/c\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

// Токен — единственное доказательство, что вы участник канала, поэтому храним его
// на устройстве: после перезагрузки страницы входим тем же токеном
function tokenKey(slug) {
  return `copypaste:channel:${slug}`;
}

function loadToken(slug) {
  try {
    return localStorage.getItem(tokenKey(slug));
  } catch {
    return null;
  }
}

function saveToken(slug, token) {
  try {
    localStorage.setItem(tokenKey(slug), token);
  } catch {
    // без localStorage канал работает, но не переживёт перезагрузку страницы
  }
}

function forgetToken(slug) {
  try {
    localStorage.removeItem(tokenKey(slug));
  } catch {
    // нечего удалять
  }
}

async function startChannel() {
  chooseChannelBtn.disabled = true;
  try {
    const response = await fetch(CHANNEL_API, { method: "POST" });
    if (!response.ok) {
      showStatus(`Не удалось создать канал (${response.status})`, "error");
      return;
    }
    const created = await response.json();
    saveToken(created.slug, created.token);
    history.pushState(null, "", `/c/${encodeURIComponent(created.slug)}`);
    route();
  } catch {
    showStatus("Не удалось связаться с сервером", "error");
  } finally {
    chooseChannelBtn.disabled = false;
  }
}

async function openChannel(slug) {
  leaveChannel();
  channel.slug = slug;
  render();

  let token = loadToken(slug);
  if (!token) {
    token = await joinChannel(slug);
    // Пока ждали ответ, пользователь мог уйти со страницы канала
    if (!token || channel.slug !== slug) {
      return;
    }
  }
  channel.token = token;
  connect();
}

async function joinChannel(slug) {
  try {
    const response = await fetch(`${CHANNEL_API}/${encodeURIComponent(slug)}/join`, {
      method: "POST",
    });
    if (response.status === 404) {
      showClosed("Канал не найден", "Он закрылся из-за бездействия или ссылка неверная.");
      return null;
    }
    if (response.status === 409) {
      showClosed("Канал уже занят", "К этому каналу уже подключены два устройства.");
      return null;
    }
    if (!response.ok) {
      showClosed("Не удалось подключиться", `Ошибка сервера (${response.status}).`);
      return null;
    }
    const joined = await response.json();
    saveToken(slug, joined.token);
    return joined.token;
  } catch {
    showClosed("Не удалось подключиться", "Нет связи с сервером.", "Повторить", () =>
      openChannel(slug),
    );
    return null;
  }
}

function leaveChannel() {
  const ws = channel.ws;
  channel.ws = null;
  if (ws) {
    ws.close();
  }
  clearTimeout(channel.retryTimer);
  clearInterval(channel.tickTimer);
  Object.assign(channel, {
    slug: null,
    token: null,
    role: null,
    peerJoined: false,
    peerOnline: false,
    connection: "",
    retries: 0,
    retryTimer: null,
    tickTimer: null,
    closedAction: null,
  });
  channel.messages.clear();
  feedEl.replaceChildren();
  for (const upload of channel.uploads) {
    upload.state = "removed";
    upload.request?.abort();
  }
  channel.uploads = [];
  channelUploadsEl.replaceChildren();
  channelClosedEl.hidden = true;
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(
    `${protocol}://${location.host}/ws/channels/${encodeURIComponent(channel.slug)}`,
  );
  channel.ws = ws;
  channel.tickTimer ??= setInterval(tick, 1000);
  setConnection("Подключение…");

  // Токен первым сообщением, а не в адресе: адреса попадают в логи
  ws.addEventListener("open", () => {
    ws.send(JSON.stringify({ type: "auth", token: channel.token }));
  });
  ws.addEventListener("message", (event) => {
    if (channel.ws === ws) {
      handleEvent(JSON.parse(event.data));
    }
  });
  ws.addEventListener("close", (event) => {
    // Соединение, которое мы закрыли сами (ушли со страницы), уже не наше
    if (channel.ws === ws) {
      channel.ws = null;
      handleClose(event.code);
    }
  });
}

function handleEvent(event) {
  switch (event.type) {
    case "state":
      channel.retries = 0;
      channel.role = event.role;
      channel.peerJoined = event.peer_joined;
      channel.peerOnline = event.peer_online;
      event.messages.forEach(addMessage);
      setConnection("");
      flushUploads();
      break;
    case "message":
      addMessage(event);
      if (event.file) {
        forgetUpload(event.file.id);
      }
      break;
    case "file_downloaded": {
      const entry = channel.messages.get(event.message_id);
      entry?.card?.markReceived(isOwnFile(entry.data) ? "Получатель скачал файл" : "Скачано");
      break;
    }
    case "roles_changed":
      channel.role = event.role;
      showStatus(event.role === "sender" ? "Теперь вы отправитель" : "Теперь вы получатель");
      break;
    case "peer_status":
      channel.peerOnline = event.online;
      channel.peerJoined = channel.peerJoined || event.online;
      break;
    case "error":
      showStatus(errorText(event), "error");
      if (event.code === "not_sender" || event.code === "file_not_found") {
        failOldestSentUpload(errorText(event));
      }
      break;
    case "channel_closed":
      // Следом сервер закроет соединение с кодом 4410 — там и покажем экран
      break;
  }
  render();
}

function errorText(event) {
  switch (event.code) {
    case "not_sender":
      return "Отправлять может только отправитель";
    case "not_receiver":
      return "Вы уже отправитель";
    case "rate_limited":
      return "Слишком часто — подождите секунду";
    case "file_not_found":
      return "Файл не отправлен: его уже отправили или загрузка устарела";
    case "invalid_event":
      return `Сервер не принял сообщение: ${event.detail}`;
    default:
      return event.detail;
  }
}

function handleClose(code) {
  const slug = channel.slug;
  switch (code) {
    case CLOSE_CHANNEL_CLOSED:
      forgetToken(slug);
      showClosed("Канал закрыт", "Канал закрылся из-за бездействия.");
      return;
    case CLOSE_NOT_FOUND:
      forgetToken(slug);
      showClosed("Канал не найден", "Он закрылся из-за бездействия или ссылка неверная.");
      return;
    case CLOSE_UNAUTHORIZED:
      forgetToken(slug);
      showClosed("Нет доступа к каналу", "Сохранённый ключ участника не подошёл.");
      return;
    case CLOSE_REPLACED:
      showClosed(
        "Канал открыт в другой вкладке",
        "Одно устройство — одно подключение. Здесь канал отключён.",
        "Открыть здесь",
        () => {
          channelClosedEl.hidden = true;
          connect();
        },
      );
      return;
  }
  if (code === CLOSE_MESSAGE_TOO_BIG) {
    showStatus("Сообщение слишком большое — сервер его не принял", "error");
  }
  // Обрыв связи (сон телефона, смена сети): переподключаемся с растущей паузой
  const delay = Math.min(1000 * 2 ** channel.retries, RECONNECT_MAX_DELAY);
  channel.retries += 1;
  setConnection(`Нет связи, переподключение через ${Math.round(delay / 1000)} с…`);
  channel.retryTimer = setTimeout(connect, delay);
}

function showClosed(title, text, actionLabel = "Начать новый обмен", action = startChannel) {
  clearInterval(channel.tickTimer);
  channel.tickTimer = null;
  channel.role = null;
  closedTitleEl.textContent = title;
  closedTextEl.textContent = text;
  closedActionBtn.textContent = actionLabel;
  channel.closedAction = action;
  channelClosedEl.hidden = false;
  setConnection("");
  render();
}

function setConnection(text) {
  channel.connection = text;
  render();
}

function connectionText(active) {
  if (channel.connection || !active || !channel.peerJoined) {
    return channel.connection;
  }
  return channel.peerOnline ? "Второе устройство в сети" : "Второе устройство не в сети";
}

function render() {
  const active = channel.role !== null && channelClosedEl.hidden;
  const isSender = channel.role === "sender";

  channelRoleEl.hidden = !active;
  channelRoleEl.textContent = isSender ? "Вы отправитель" : "Вы получатель";
  channelRoleEl.classList.toggle("sender", isSender);

  becomeSenderBtn.hidden = !active || isSender;
  becomeSenderBtn.disabled = !channel.ws;
  channelComposeEl.hidden = !active || !isSender;
  channelSendBtn.disabled = !channel.ws;

  // QR нужен, только пока второе устройство не подключалось: третьего канал не пустит
  const inviting = active && !channel.peerJoined;
  channelInviteEl.hidden = !inviting;
  if (inviting && channelUrlEl.dataset.slug !== channel.slug) {
    channelUrlEl.dataset.slug = channel.slug;
    channelUrlEl.value = `${location.origin}/c/${encodeURIComponent(channel.slug)}`;
    channelQrEl.src = `${CHANNEL_API}/${encodeURIComponent(channel.slug)}/qr`;
    channelCodeEl.textContent = channel.slug;
  }
  channelConnEl.textContent = connectionText(active);

  feedEl.hidden = !channelClosedEl.hidden;
  feedEmptyEl.hidden = !active || channel.messages.size > 0;
}

// Ссылок нет только у своего файла: скачивает его собеседник
function isOwnFile(message) {
  return message.kind === "file" && !message.file?.download_url;
}

function addMessage(message) {
  if (channel.messages.has(message.id)) {
    return;
  }
  const item = document.createElement("li");
  item.className = "feed-item";

  const meta = document.createElement("div");
  meta.className = "feed-meta";
  const left = document.createElement("span");
  left.className = "feed-left";
  meta.append(left);

  let card = null;
  if (message.kind === "file" && message.file) {
    card = createFileCard(message.file, {
      status: isOwnFile(message) ? "Ждёт скачивания получателем" : "",
    });
    item.append(card.el, meta);
  } else {
    const text = document.createElement("pre");
    text.className = "feed-text";
    text.textContent = message.text;
    const copy = document.createElement("button");
    copy.className = "button";
    copy.type = "button";
    copy.textContent = "Скопировать";
    copy.addEventListener("click", () => copyMessage(message.text));
    meta.append(copy);
    item.append(text, meta);
  }
  // Новые сверху: обычно нужно именно последнее присланное
  const older = [...channel.messages.values()]
    .filter((m) => m.data.id < message.id)
    .sort((a, b) => b.data.id - a.data.id)[0];
  feedEl.insertBefore(item, older ? older.el : null);
  channel.messages.set(message.id, { data: message, el: item, left, card });
  updateLeft(channel.messages.get(message.id));
}

function updateLeft(entry) {
  const seconds = Math.ceil((Date.parse(entry.data.expires_at) - Date.now()) / 1000);
  if (seconds <= 0) {
    entry.el.remove();
    channel.messages.delete(entry.data.id);
    return;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = minutes ? `${minutes} мин ${seconds % 60} с` : `${seconds} с`;
  entry.left.textContent = `исчезнет через ${rest}`;
}

function tick() {
  [...channel.messages.values()].forEach(updateLeft);
  render();
}

async function copyMessage(text) {
  try {
    await copyToClipboard(text);
    showStatus("Текст скопирован");
  } catch {
    showStatus("Не удалось скопировать", "error");
  }
}

function sendMessage() {
  const text = channelTextEl.value;
  if (!text.trim()) {
    showStatus("Нечего отправлять — поле пустое", "error");
    return;
  }
  if (!channel.ws || channel.ws.readyState !== WebSocket.OPEN) {
    showStatus("Нет связи с каналом", "error");
    return;
  }
  channel.ws.send(JSON.stringify({ type: "send", text }));
  channelTextEl.value = "";
  updateChannelCounter();
  channelTextEl.focus();
}

function wsOpen() {
  return channel.ws !== null && channel.ws.readyState === WebSocket.OPEN;
}

function sendFiles(files) {
  for (const file of files) {
    const upload = startUpload(file, {
      url: () => `${CHANNEL_API}/${encodeURIComponent(channel.slug)}/files`,
      headers: () => ({ "X-Participant-Token": channel.token }),
      list: channelUploadsEl,
      onDone: flushUploads,
      onRemoved: (removed) => {
        channel.uploads = channel.uploads.filter((item) => item !== removed);
      },
    });
    channel.uploads.push(upload);
  }
}

// Загруженные файлы отправляем в канал; без связи — дождутся переподключения (state)
function flushUploads() {
  for (const upload of channel.uploads) {
    if (upload.state !== "done" || upload.sent) {
      continue;
    }
    if (!wsOpen()) {
      upload.status.textContent = "Ждёт связи…";
      continue;
    }
    channel.ws.send(JSON.stringify({ type: "send_file", file_id: upload.id }));
    upload.sent = true;
    upload.status.textContent = "Отправляется…";
  }
}

// Файл дошёл до канала и уже виден в ленте — из списка загрузок он больше не нужен
function forgetUpload(fileId) {
  const upload = channel.uploads.find((item) => item.id === fileId);
  if (upload) {
    upload.el.remove();
    channel.uploads = channel.uploads.filter((item) => item !== upload);
  }
}

// Сервер отвечает на send_file по порядку, а дошедшие файлы уже убраны из списка
// (forgetUpload) — значит, ошибка относится к самому раннему ещё не подтверждённому
function failOldestSentUpload(reason) {
  const upload = channel.uploads.find((item) => item.sent);
  if (upload) {
    upload.state = "error";
    upload.sent = false;
    upload.status.textContent = reason;
  }
}

function becomeSender() {
  if (channel.ws && channel.ws.readyState === WebSocket.OPEN) {
    channel.ws.send(JSON.stringify({ type: "become_sender" }));
  }
}

function updateChannelCounter() {
  channelCounterEl.textContent = `${channelTextEl.value.length.toLocaleString("ru-RU")} / ${CHANNEL_MAX_LENGTH.toLocaleString("ru-RU")}`;
}

// Принимает и код, и вставленную целиком ссылку; регистр и пробелы прощаем
function parseChannelCode(value) {
  const fromLink = value.match(/\/c\/([^/?#\s]+)/);
  const raw = fromLink ? decodeURIComponent(fromLink[1]) : value;
  const code = raw.toLowerCase().replace(/\s+/g, "");
  return SLUG_PATTERN.test(code) ? code : null;
}

function joinByCode(event) {
  event.preventDefault();
  const code = parseChannelCode(joinCodeEl.value);
  if (!code) {
    showStatus("Код — 8 символов: латинские буквы и цифры", "error");
    joinCodeEl.focus();
    return;
  }
  joinCodeEl.value = "";
  history.pushState(null, "", `/c/${code}`);
  route();
}

async function copyChannelUrl() {
  try {
    await copyToClipboard(channelUrlEl.value);
    showStatus("Ссылка скопирована");
  } catch {
    showStatus("Не удалось скопировать ссылку", "error");
  }
}

// Уходя со страницы, браузер может спрятать её в кэш «назад/вперёд» вместе с открытым
// соединением — тогда собеседник так и видел бы нас в сети
window.addEventListener("pagehide", leaveChannel);
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    route();
  }
});

chooseChannelBtn.addEventListener("click", startChannel);
joinFormEl.addEventListener("submit", joinByCode);
becomeSenderBtn.addEventListener("click", becomeSender);
channelSendBtn.addEventListener("click", sendMessage);
bindFilePicker(channelAttachBtn, channelAttachInput, sendFiles);
acceptDroppedAndPasted(channelComposeEl, channelTextEl, sendFiles);
channelUrlCopyBtn.addEventListener("click", copyChannelUrl);
channelTextEl.addEventListener("input", updateChannelCounter);
closedActionBtn.addEventListener("click", () => channel.closedAction?.());
channelTextEl.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    sendMessage();
  }
});

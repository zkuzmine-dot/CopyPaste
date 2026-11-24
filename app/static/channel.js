// Канал: обмен текстом и файлами между двумя устройствами через WebSocket.
// Использует showStatus и copyToClipboard из app.js, функции файлов из files.js, ключи из
// channel-crypto.js и channel-keystore.js — они нужны только во время работы.

const CHANNEL_API = "/api/channels";
const CHANNEL_MAX_LENGTH = 100000;
const RECONNECT_MAX_DELAY = 15000;
const CLOSE_MESSAGE_TOO_BIG = 1009;
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_NOT_FOUND = 4404;
const CLOSE_REPLACED = 4409;
const CLOSE_CHANNEL_CLOSED = 4410;
const SLUG_PATTERN = /^[23456789abcdefghjkmnpqrstuvwxyz]{8}$/;
const KEYS_PENDING_TEXT = "Подождите: устройства ещё обмениваются ключами";
const SHAPES_HINT_KEY = "copypaste:shapes-hint-seen";

const chooseChannelBtn = document.getElementById("choose-channel");
const navChannelBtn = document.getElementById("nav-channel");
const channelBarEl = document.getElementById("channel-bar");
const channelRoleEl = document.getElementById("channel-role");
const channelConnEl = document.getElementById("channel-conn");
const channelSecureEl = document.getElementById("channel-secure");
const channelSealBtn = document.getElementById("channel-seal");
const channelSecureTextEl = document.getElementById("channel-secure-text");
const channelShapesEl = document.getElementById("channel-shapes");
const channelHintEl = document.getElementById("channel-hint");
const channelHintOkBtn = document.getElementById("channel-hint-ok");
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
const feedSectionEl = document.getElementById("feed-section");
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
  // Запись из channel-keystore.js и то, что о ключах знает сервер (из state и событий)
  keys: null,
  keySent: false,
  peerCommitment: null,
  peerKey: null,
  // Общие ключи и фигуры. Пока их нет, писать в канал нельзя
  cipher: null,
  // Шаги обмена асинхронные, а события приходят подряд — выполняем шаги по очереди
  handshake: Promise.resolve(),
};

// Порядок — как у значений из verificationShapes: shape — номер строки, filled — заливка
const SHAPES = [
  ["круг", "m", '<circle cx="12" cy="12" r="9.5"/>'],
  ["квадрат", "m", '<rect x="3" y="3" width="18" height="18" rx="1"/>'],
  ["треугольник", "m", '<polygon points="12,2.5 22,20.5 2,20.5"/>'],
  ["ромб", "m", '<polygon points="12,1.5 22.5,12 12,22.5 1.5,12"/>'],
  ["пятиугольник", "m", '<polygon points="12,2.3 21.7,9.3 18,20.8 6,20.8 2.3,9.3"/>'],
  ["шестиугольник", "m", '<polygon points="12,1.8 20.9,6.9 20.9,17.1 12,22.2 3.1,17.1 3.1,6.9"/>'],
  [
    "звезда",
    "f",
    '<polygon points="12,1.8 14.6,9.1 22.4,9.3 16.2,14.1 18.4,21.6 12,17.2 5.6,21.6 7.8,14.1 1.6,9.3 9.4,9.1"/>',
  ],
  [
    "плюс",
    "m",
    '<polygon points="8.8,2 15.2,2 15.2,8.8 22,8.8 22,15.2 15.2,15.2 15.2,22 8.8,22 8.8,15.2 2,15.2 2,8.8 8.8,8.8"/>',
  ],
  [
    "крестик",
    "m",
    '<polygon transform="rotate(45 12 12)" points="9.1,2.5 14.9,2.5 14.9,9.1 21.5,9.1 21.5,14.9 14.9,14.9 14.9,21.5 9.1,21.5 9.1,14.9 2.5,14.9 2.5,9.1 9.1,9.1"/>',
  ],
  [
    "сердце",
    "n",
    '<path d="M12 21.5C12 21.5 2.5 15 2.5 8.6A4.9 4.9 0 0 1 12 6.6A4.9 4.9 0 0 1 21.5 8.6C21.5 15 12 21.5 12 21.5Z"/>',
  ],
  ["полумесяц", "m", '<path d="M12.38 3.01A9 9 0 1 0 20.74 14.15A7 7 0 1 1 12.38 3.01Z"/>'],
  ["стрелка", "f", '<polygon points="12,1.8 21.5,11 15.6,11 15.6,22 8.4,22 8.4,11 2.5,11"/>'],
  ["полукруг", "m", '<path d="M2.5 17A9.5 9.5 0 0 1 21.5 17Z"/>'],
  [
    "капля",
    "f",
    '<path d="M12 1.8C12 1.8 4.6 10.6 4.6 15A7.4 7.4 0 0 0 19.4 15C19.4 10.6 12 1.8 12 1.8Z"/>',
  ],
  ["молния", "f", '<polygon points="14.5,1.5 4,13.5 10.8,13.5 8.8,22.5 20,9.8 13.2,9.8 16,1.5"/>'],
  ["трапеция", "f", '<polygon points="7,4.5 17,4.5 22.5,19.5 1.5,19.5"/>'],
];
const SHAPE_FILL = {
  true: { m: "залитый", f: "залитая", n: "залитое" },
  false: { m: "контурный", f: "контурная", n: "контурное" },
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

function setStarting(starting) {
  chooseChannelBtn.disabled = starting;
  navChannelBtn.disabled = starting;
}

function newKeyRecord(role, keys) {
  return { role, ...keys, revealedTo: null, peerKey: null, createdAt: Date.now() };
}

async function startChannel() {
  if (!cryptoAvailable()) {
    showStatus("Канал работает только по HTTPS", "error");
    return;
  }
  setStarting(true);
  try {
    const keys = await createDeviceKeys();
    const response = await fetch(CHANNEL_API, { method: "POST" });
    if (!response.ok) {
      showStatus(msg("Не удалось создать канал ({status})", { status: String(response.status) }), "error");
      return;
    }
    const created = await response.json();
    await saveChannelKeys(created.slug, newKeyRecord("creator", keys));
    saveToken(created.slug, created.token);
    history.pushState(null, "", `/c/${encodeURIComponent(created.slug)}`);
    route();
  } catch {
    showStatus("Нет связи с сервером", "error");
  } finally {
    setStarting(false);
  }
}

async function openChannel(slug) {
  leaveChannel();
  channel.slug = slug;
  render();
  if (!cryptoAvailable()) {
    showClosed(
      "Нужен защищённый адрес",
      "Канал шифрует сообщения в браузере, а браузер даёт шифрование только по HTTPS.",
      "На главную",
      () => {
        history.pushState(null, "", "/");
        route();
      },
    );
    return;
  }
  pruneChannelKeys();

  let token = loadToken(slug);
  let keys;
  if (token) {
    keys = await loadChannelKeys(slug);
    if (channel.slug !== slug) {
      return;
    }
    if (!keys) {
      forgetToken(slug);
      showClosed(
        "Нет ключа канала",
        "Ключ хранится только в браузере, где канал открыли, а здесь его нет.",
      );
      return;
    }
  } else {
    const joined = await joinChannel(slug);
    // Пока ждали ответ, пользователь мог уйти со страницы канала
    if (!joined || channel.slug !== slug) {
      return;
    }
    ({ token, keys } = joined);
  }
  channel.token = token;
  channel.keys = keys;
  connect();
}

async function joinChannel(slug) {
  try {
    const keys = await createDeviceKeys();
    const commitment = toBase64Url(await keyCommitment(keys.publicKey));
    const response = await fetch(`${CHANNEL_API}/${encodeURIComponent(slug)}/join`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key_commitment: commitment }),
    });
    if (response.status === 404) {
      showClosed("Канал не найден", "Он закрылся из-за бездействия или в ссылке ошибка.");
      return null;
    }
    if (response.status === 409) {
      showClosed("Канал уже занят", "К этому каналу уже подключены два устройства.");
      return null;
    }
    if (!response.ok) {
      showClosed(
        "Не удалось подключиться",
        msg("Ошибка сервера ({status}).", { status: String(response.status) }),
      );
      return null;
    }
    const joined = await response.json();
    const record = newKeyRecord("joiner", keys);
    await saveChannelKeys(slug, record);
    saveToken(slug, joined.token);
    return { token: joined.token, keys: record };
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
    keys: null,
    keySent: false,
    peerCommitment: null,
    peerKey: null,
    cipher: null,
    handshake: Promise.resolve(),
  });
  channelShapesEl.replaceChildren();
  setHintOpen(false);
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
      channel.keySent = event.key_sent;
      channel.peerCommitment = event.peer_key_commitment;
      channel.peerKey = event.peer_public_key;
      event.messages.forEach(addMessage);
      setConnection("");
      flushUploads();
      queueHandshake();
      break;
    case "peer_joined":
      channel.peerJoined = true;
      channel.peerCommitment = event.key_commitment;
      queueHandshake();
      break;
    case "peer_key":
      channel.peerKey = event.key;
      queueHandshake();
      break;
    case "message":
      addMessage(event);
      if (event.file) {
        forgetUpload(event.file.id);
      }
      break;
    case "file_downloaded": {
      const entry = channel.messages.get(event.message_id);
      if (entry) {
        entry.receivedText = isOwnFile(entry.data) ? "Получатель скачал файл" : "Скачано";
        entry.card?.markReceived(entry.receivedText);
      }
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
      if (event.code === "key_mismatch") {
        keysMismatch();
        return;
      }
      // Ключ уже у сервера: например, отправили его ещё раз сразу после переподключения
      if (event.code === "key_already_sent") {
        break;
      }
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

// Ошибка от сервера → msg(). Неизвестный код показываем как есть: detail сервер пишет по-английски
function errorText(event) {
  switch (event.code) {
    case "not_sender":
      return msg("Отправлять может только отправитель");
    case "not_receiver":
      return msg("Вы уже отправитель");
    case "rate_limited":
      return msg("Слишком часто. Подождите секунду");
    case "file_not_found":
      return msg("Файл не отправлен: его уже отправили или загрузка устарела");
    case "keys_not_ready":
      return msg(KEYS_PENDING_TEXT);
    case "key_out_of_order":
      return msg("Обмен ключами сбился. Обновите страницу");
    case "invalid_event":
      return msg("Сервер не принял сообщение: {detail}", { detail: event.detail });
    default:
      return () => event.detail;
  }
}

function forgetChannel(slug) {
  forgetToken(slug);
  forgetChannelKeys(slug);
}

function handleClose(code) {
  const slug = channel.slug;
  switch (code) {
    case CLOSE_CHANNEL_CLOSED:
      forgetChannel(slug);
      showClosed("Канал закрыт", "Канал закрылся из-за бездействия.");
      return;
    case CLOSE_NOT_FOUND:
      forgetChannel(slug);
      showClosed("Канал не найден", "Он закрылся из-за бездействия или в ссылке ошибка.");
      return;
    case CLOSE_UNAUTHORIZED:
      forgetChannel(slug);
      showClosed("Нет доступа к каналу", "Это устройство больше не участник канала.");
      return;
    case CLOSE_REPLACED:
      showClosed(
        "Канал открыт в другой вкладке",
        "Здесь он отключён: одно устройство подключается только из одной вкладки.",
        "Открыть здесь",
        () => {
          channelClosedEl.hidden = true;
          connect();
        },
      );
      return;
  }
  if (code === CLOSE_MESSAGE_TOO_BIG) {
    showStatus("Сообщение слишком большое, сервер его не принял", "error");
  }
  // Обрыв связи (сон телефона, смена сети): переподключаемся с растущей паузой
  const delay = Math.min(1000 * 2 ** channel.retries, RECONNECT_MAX_DELAY);
  channel.retries += 1;
  setConnection(msg("Нет связи, переподключение через {seconds} с…", { seconds: Math.round(delay / 1000) }));
  channel.retryTimer = setTimeout(connect, delay);
}

function showClosed(title, text, actionLabel = "Открыть новый канал", action = startChannel) {
  clearInterval(channel.tickTimer);
  channel.tickTimer = null;
  channel.role = null;
  setText(closedTitleEl, title);
  setText(closedTextEl, text);
  setText(closedActionBtn, actionLabel);
  channel.closedAction = action;
  channelClosedEl.hidden = false;
  setConnection("");
  render();
}

function setConnection(text) {
  channel.connection = text;
  render();
}

function queueHandshake() {
  const slug = channel.slug;
  channel.handshake = channel.handshake
    .then(() => advanceHandshake(slug))
    .catch(() => {
      if (channel.slug === slug) {
        showStatus("Не удалось обменяться ключами", "error");
      }
    });
}

// Делает следующий шаг обмена из того, что уже известно о ключах (README, «Обмен ключами»)
async function advanceHandshake(slug) {
  let keys = channel.keys;
  if (channel.slug !== slug || !keys) {
    return;
  }
  // Ключ собеседника не меняется, а создатель раскрывает свой одному отпечатку за канал.
  // Иначе сервер, уже зная ключ создателя, мог бы прислать второй отпечаток, подобранный под
  // совпадающие фигуры. Расхождение после переподключения — подмена
  const commitmentChanged =
    keys.revealedTo && channel.peerCommitment && keys.revealedTo !== channel.peerCommitment;
  if ((keys.peerKey && channel.peerKey !== keys.peerKey) || commitmentChanged) {
    keysMismatch();
    return;
  }
  if (channel.cipher) {
    return;
  }
  if (keys.role === "creator") {
    if (!channel.peerCommitment) {
      return;
    }
    if (!keys.revealedTo) {
      keys = { ...keys, revealedTo: channel.peerCommitment };
      await saveChannelKeys(slug, keys);
      if (channel.slug !== slug) {
        return;
      }
      channel.keys = keys;
    }
    if (!channel.keySent && !sendPublicKey(keys)) {
      return;
    }
    if (!channel.peerKey) {
      return;
    }
    const peerKey = fromBase64Url(channel.peerKey);
    if (!(await matchesCommitment(peerKey, fromBase64Url(keys.revealedTo)))) {
      keysMismatch();
      return;
    }
  } else {
    if (!channel.peerKey) {
      return;
    }
    if (!channel.keySent && !sendPublicKey(keys)) {
      return;
    }
  }

  const peerKey = fromBase64Url(channel.peerKey);
  const [creatorKey, joinerKey] =
    keys.role === "creator" ? [keys.publicKey, peerKey] : [peerKey, keys.publicKey];
  const cipher = await deriveChannelCipher(keys.privateKey, peerKey, {
    slug,
    creatorKey,
    joinerKey,
  });
  if (!keys.peerKey) {
    keys = { ...keys, peerKey: channel.peerKey };
    await saveChannelKeys(slug, keys);
  }
  if (channel.slug !== slug) {
    return;
  }
  channel.keys = keys;
  channel.cipher = cipher;
  showShapes(cipher.shapes);
  channel.messages.forEach(reveal);
  render();
  offerShapesHint();
  flushUploads();
}

function sendPublicKey(keys) {
  if (!wsOpen()) {
    return false;
  }
  channel.ws.send(JSON.stringify({ type: "public_key", key: toBase64Url(keys.publicKey) }));
  channel.keySent = true;
  return true;
}

function keysMismatch() {
  const slug = channel.slug;
  forgetChannel(slug);
  clearTimeout(channel.retryTimer);
  const ws = channel.ws;
  channel.ws = null;
  ws?.close();
  showClosed(
    "Ключи не сошлись",
    "Кто-то мог вмешаться в обмен ключами, поэтому этот канал небезопасен. Откройте новый.",
  );
}

function shapeLabel({ shape, filled }) {
  const [name, gender] = SHAPES[shape];
  return `${t(name)}, ${t(SHAPE_FILL[filled][gender])}`;
}

function showShapes(shapes) {
  channelShapesEl.replaceChildren(
    ...shapes.map((item) => {
      const span = document.createElement("span");
      span.className = "seal-shape";
      const paint = item.filled
        ? 'fill="currentColor"'
        : 'fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"';
      // Разметка из постоянной таблицы SHAPES, данных собеседника в ней нет
      span.innerHTML = `<svg role="img" viewBox="0 0 24 24" width="20" height="20"><g ${paint}>${SHAPES[item.shape][2]}</g></svg>`;
      setAttr(span.firstElementChild, "aria-label", () => shapeLabel(item));
      return span;
    }),
  );
}

function setHintOpen(open) {
  channelHintEl.hidden = !open;
  channelSealBtn.setAttribute("aria-expanded", String(open));
}

// Подсказка всплывает сама один раз на устройстве, дальше её открывает капсула
function offerShapesHint() {
  if (!channelSecureEl.hidden && localStorage.getItem(SHAPES_HINT_KEY) === null) {
    setHintOpen(true);
  }
}

function dismissHint() {
  setHintOpen(false);
  localStorage.setItem(SHAPES_HINT_KEY, "1");
}

// От состояния зависит точка перед текстом: зелёная — в сети, янтарная — нет связи
function connectionState(active) {
  if (channel.connection) {
    return channel.retries > 0 ? "problem" : "connecting";
  }
  if (!active) {
    return "";
  }
  if (!channel.peerJoined) {
    return "waiting";
  }
  return channel.peerOnline ? "online" : "offline";
}

function connectionText(state) {
  switch (state) {
    case "waiting":
      return msg("Ждём второе устройство");
    case "online":
      return msg("Второе устройство в сети");
    case "offline":
      return msg("Второе устройство не в сети");
    default:
      return channel.connection;
  }
}

function render() {
  const active = channel.role !== null && channelClosedEl.hidden;
  const isSender = channel.role === "sender";

  channelRoleEl.hidden = !active;
  setText(channelRoleEl, isSender ? "Вы отправитель" : "Вы получатель");
  channelRoleEl.classList.toggle("sender", isSender);

  becomeSenderBtn.hidden = !active || isSender;
  becomeSenderBtn.disabled = !channel.ws;
  channelComposeEl.hidden = !active || !isSender;
  channelSendBtn.disabled = !channel.ws || !channel.cipher;
  channelAttachBtn.disabled = !channel.cipher;

  // До второго устройства обмениваться ключами не с кем — об этом уже говорит приглашение
  const ready = Boolean(channel.cipher);
  channelSecureEl.hidden = !active || !channel.peerJoined;
  channelSealBtn.dataset.state = ready ? "ready" : "pending";
  channelSealBtn.disabled = !ready;
  if (ready) {
    setText(channelSecureTextEl, "Зашифровано");
  } else if (channel.peerOnline) {
    setText(channelSecureTextEl, "Обмен ключами…");
  } else {
    setText(channelSecureTextEl, "Обмен ключами: ждём второе устройство");
  }
  if (channelSecureEl.hidden) {
    setHintOpen(false);
  }

  // QR нужен, только пока второе устройство не подключалось: третьего канал не пустит
  const inviting = active && !channel.peerJoined;
  channelInviteEl.hidden = !inviting;
  if (inviting && channelUrlEl.dataset.slug !== channel.slug) {
    channelUrlEl.dataset.slug = channel.slug;
    channelUrlEl.value = `${location.origin}/c/${encodeURIComponent(channel.slug)}`;
    channelQrEl.src = `${CHANNEL_API}/${encodeURIComponent(channel.slug)}/qr`;
    // Две группы по четыре — так код легче продиктовать. Пробел рисует CSS, а не текст:
    // скопированный код остаётся слитным
    channelCodeEl.replaceChildren(
      el("span", null, channel.slug.slice(0, 4)),
      el("span", null, channel.slug.slice(4)),
    );
  }
  const state = connectionState(active);
  channelConnEl.dataset.state = state;
  setText(channelConnEl, connectionText(state));
  // Над экраном «канал закрыт» пустая строка статуса дала бы лишний отступ
  channelBarEl.hidden = !active && !state;

  feedSectionEl.hidden = !active;
  feedEmptyEl.hidden = channel.messages.size > 0;
}

// Ссылок нет только у своего файла: скачивает его собеседник
function isOwnFile(message) {
  return message.kind === "file" && !message.file?.download_url;
}

function addMessage(message) {
  if (channel.messages.has(message.id)) {
    return;
  }
  // Строка ленты: время | содержимое и срок | действие (у файла кнопки внутри карточки)
  const item = el("li", "feed-item");
  const time = el("time", "feed-time", () =>
    new Date(message.created_at).toLocaleTimeString(currentLocale(), {
      hour: "2-digit",
      minute: "2-digit",
    }),
  );
  time.dateTime = message.created_at;
  const body = el("div", "feed-body");
  const left = el("span", "feed-left");
  item.append(time, body);

  const isText = message.kind === "text";
  // Файла нет в сообщении, когда его уже скачали и удалили с сервера
  const gone = !isText && !message.file;
  const placeholder = gone
    ? el("pre", "feed-text gone", msg("Файл больше недоступен"))
    : el("pre", "feed-text pending", "…");
  body.append(placeholder, left);
  let copy = null;
  if (isText) {
    copy = el("button", "button", msg("Скопировать"));
    copy.type = "button";
    copy.hidden = true;
    item.append(copy);
  } else {
    item.classList.add("file");
  }
  // Новые сверху: обычно нужно именно последнее присланное
  const older = [...channel.messages.values()]
    .filter((m) => m.data.id < message.id)
    .sort((a, b) => b.data.id - a.data.id)[0];
  feedEl.insertBefore(item, older ? older.el : null);
  const entry = {
    data: message,
    el: item,
    left,
    placeholder,
    copy,
    text: null,
    card: null,
    // file_downloaded может прийти раньше, чем расшифровано описание файла
    receivedText: null,
    opening: gone,
  };
  channel.messages.set(message.id, entry);
  copy?.addEventListener("click", () => copyMessage(entry.text));
  updateLeft(entry);
  reveal(entry);
}

// До общего ключа сообщение ждёт: после перезагрузки state приходит раньше, чем он посчитан
async function reveal(entry) {
  const cipher = channel.cipher;
  if (!cipher || entry.opening) {
    return;
  }
  entry.opening = true;
  const slug = channel.slug;
  const message = entry.data;
  try {
    const payload = fromBase64Url(message.payload);
    if (message.kind === "text") {
      entry.text = await openText(cipher.textKey, slug, payload);
      setPlainText(entry.placeholder, entry.text);
      entry.placeholder.classList.remove("pending");
      entry.copy.hidden = false;
      return;
    }
    const meta = await openFileMeta(cipher.textKey, slug, payload);
    // Имя и размер — из описания; у сервера только размер шифротекста
    const file = { ...message.file, name: meta.name, size: meta.size, stored_size: message.file.size };
    entry.card = createFileCard(file, {
      status: isOwnFile(message) ? "Ждёт скачивания" : "",
      decrypt: (sealed) => openFile(cipher.fileKey, slug, meta.nonce, sealed),
    });
    entry.placeholder.replaceWith(entry.card.el);
    if (entry.receivedText) {
      entry.card.markReceived(entry.receivedText);
    }
  } catch {
    setText(
      entry.placeholder,
      message.kind === "text"
        ? "Не удалось расшифровать сообщение"
        : "Не удалось расшифровать описание файла",
    );
    entry.placeholder.classList.replace("pending", "undecryptable");
  }
}

function updateLeft(entry) {
  const seconds = Math.ceil((Date.parse(entry.data.expires_at) - Date.now()) / 1000);
  if (seconds <= 0) {
    entry.el.remove();
    channel.messages.delete(entry.data.id);
    return;
  }
  const rest = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  setText(entry.left, "исчезнет через {rest}", { rest });
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

async function sendMessage() {
  const text = channelTextEl.value;
  if (!text.trim()) {
    showStatus("Нечего отправлять: поле пустое", "error");
    return;
  }
  if (!channel.cipher) {
    showStatus(KEYS_PENDING_TEXT, "error");
    return;
  }
  const slug = channel.slug;
  const payload = await sealText(channel.cipher.textKey, slug, text);
  if (!wsOpen() || channel.slug !== slug) {
    showStatus("Нет связи с каналом", "error");
    return;
  }
  channel.ws.send(JSON.stringify({ type: "send", payload: toBase64Url(payload) }));
  // Пока шифровали, могли дописать ещё — тогда поле не трогаем
  if (channelTextEl.value === text) {
    channelTextEl.value = "";
    updateChannelCounter();
  }
  channelTextEl.focus();
}

function wsOpen() {
  return channel.ws !== null && channel.ws.readyState === WebSocket.OPEN;
}

function sendFiles(files) {
  // Перетащить или вставить файл можно и при выключенной кнопке «Файлы»
  if (!channel.cipher) {
    showStatus(KEYS_PENDING_TEXT, "error");
    return;
  }
  for (const file of files) {
    const upload = startUpload(file, {
      url: () => `${CHANNEL_API}/${encodeURIComponent(channel.slug)}/files`,
      headers: () => ({ "X-Participant-Token": channel.token }),
      list: channelUploadsEl,
      encrypt: sealChannelFile,
      onDone: flushUploads,
      onRemoved: (removed) => {
        channel.uploads = channel.uploads.filter((item) => item !== removed);
      },
    });
    channel.uploads.push(upload);
  }
}

// Сервер получает только шифротекст; имя, размер и nonce уходят зашифрованным описанием
async function sealChannelFile(file) {
  const { slug, cipher } = channel;
  const { body, nonce } = await sealFile(cipher.fileKey, slug, file);
  const meta = await sealFileMeta(cipher.textKey, slug, {
    name: file.name || t("файл"),
    size: file.size,
    nonce,
  });
  return { body, payload: toBase64Url(meta) };
}

// Загруженные файлы отправляем в канал; без связи — дождутся переподключения (state)
function flushUploads() {
  for (const upload of channel.uploads) {
    if (upload.state !== "done" || upload.sent) {
      continue;
    }
    if (!wsOpen()) {
      setText(upload.status, "Ждёт связи…");
      continue;
    }
    channel.ws.send(
      JSON.stringify({ type: "send_file", file_id: upload.id, payload: upload.sealed.payload }),
    );
    upload.sent = true;
    setText(upload.status, "Отправляется…");
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
    setText(upload.status, reason);
  }
}

function becomeSender() {
  if (channel.ws && channel.ws.readyState === WebSocket.OPEN) {
    channel.ws.send(JSON.stringify({ type: "become_sender" }));
  }
}

function updateChannelCounter() {
  setText(channelCounterEl, "{count} / {max}", {
    count: channelTextEl.value.length,
    max: CHANNEL_MAX_LENGTH,
  });
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
    showStatus("Код: 8 латинских букв и цифр", "error");
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
// В открытом канале пункт «Канал» — текущий раздел, а не команда открыть ещё один
navChannelBtn.addEventListener("click", () => {
  if (!navChannelBtn.hasAttribute("aria-current")) {
    startChannel();
  }
});
joinFormEl.addEventListener("submit", joinByCode);
becomeSenderBtn.addEventListener("click", becomeSender);
channelSealBtn.addEventListener("click", () => {
  if (channelHintEl.hidden) {
    setHintOpen(true);
  } else {
    dismissHint();
  }
});
channelHintOkBtn.addEventListener("click", dismissHint);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !channelHintEl.hidden) {
    dismissHint();
  }
});
document.addEventListener("click", (event) => {
  if (!channelHintEl.hidden && !channelSecureEl.contains(event.target)) {
    dismissHint();
  }
});
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

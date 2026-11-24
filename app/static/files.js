// Файлы — общее для заметок и канала: выбор, перетаскивание, вставка из буфера,
// загрузка с прогрессом и карточка файла со скачиванием.
// Использует showStatus из app.js — он нужен только во время работы.

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const CONFIRM_ATTEMPTS = 3;
const CONFIRM_TIMEOUT_MS = 10000;
// Столько ждём следующей порции данных, прежде чем считать соединение зависшим
const STALL_TIMEOUT_MS = 15000;

class FileGoneError extends UiError {}
class FileDamagedError extends UiError {}

// text — готовый текст (имя файла) или msg(): тогда он переведётся при смене языка
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (typeof text === "function") {
    setText(node, text);
  } else if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function formatSize(bytes) {
  if (bytes < 1024) {
    return t("{bytes} Б", { bytes });
  }
  const units = ["КБ", "МБ", "ГБ"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value < 10 ? 1 : 0;
  return `${value.toLocaleString(currentLocale(), { maximumFractionDigits: digits })} ${t(units[unit])}`;
}

function tooLargeText() {
  return msg("Больше {size}", { size: () => formatSize(MAX_FILE_SIZE) });
}

// Проверяем до загрузки, чтобы не гонять по сети то, что сервер всё равно отклонит
function fileProblem(file) {
  if (file.size === 0) {
    return msg("Пустой файл");
  }
  if (file.size > MAX_FILE_SIZE) {
    return tooLargeText();
  }
  return null;
}

function uploadErrorText(status) {
  switch (status) {
    case 400:
      return msg("Пустой файл");
    case 401:
    case 404:
      return msg("Канал недоступен");
    case 403:
      return msg("Загружать может только отправитель");
    case 413:
      return tooLargeText();
    case 429:
      return msg("Слишком много загрузок. Подождите минуту");
    case 507:
      return msg("Хранилище переполнено");
    default:
      return msg("Ошибка сервера ({status})", { status: String(status) });
  }
}

function uploadError(render) {
  const error = new Error(render());
  error.render = render;
  return error;
}

// XMLHttpRequest, а не fetch: прогресс отправки есть только у него.
// Без name сервер имени не узнаёт — так загружается зашифрованный файл канала
function uploadFile(url, body, { name, headers = {}, onProgress } = {}) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open("POST", name === undefined ? url : `${url}?name=${encodeURIComponent(name)}`);
    for (const [name, value] of Object.entries(headers)) {
      xhr.setRequestHeader(name, value);
    }
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        onProgress?.(event.loaded / event.total);
      }
    });
    xhr.addEventListener("load", () => {
      if (xhr.status === 201) {
        resolve(JSON.parse(xhr.responseText));
      } else {
        reject(uploadError(uploadErrorText(xhr.status)));
      }
    });
    xhr.addEventListener("error", () => reject(new UiError("Нет связи с сервером")));
    xhr.addEventListener("abort", () => reject(new UiError("Загрузка отменена")));
    xhr.send(body);
  });
  return { promise, abort: () => xhr.abort() };
}

function createProgress() {
  const wrap = el("div", "progress");
  const bar = el("div", "progress-bar");
  wrap.append(bar);
  wrap.hidden = true;
  return {
    el: wrap,
    set(part) {
      wrap.hidden = false;
      bar.style.width = `${Math.round(Math.min(part, 1) * 100)}%`;
    },
    hide() {
      wrap.hidden = true;
    },
  };
}

// Одна загрузка в списке: прогресс, ошибка с повтором, отмена крестиком.
// onDone — когда файл загружен, onChange — при любой смене состояния.
// encrypt(file) → { body, payload }: загружается body, а payload остаётся в upload.sealed
function startUpload(
  file,
  { url, headers = () => ({}), list, onDone, onChange, onRemoved, encrypt },
) {
  const item = el("li", "upload-item");
  const status = el("span", "file-status");
  const progress = createProgress();
  const retry = el("button", "button small", msg("Повторить"));
  retry.type = "button";
  retry.hidden = true;
  const remove = el("button", "icon-button", "×");
  remove.type = "button";
  setAttr(remove, "aria-label", "Убрать файл");
  item.append(
    el("span", "file-name", file.name || msg("файл")),
    el("span", "file-size", () => formatSize(file.size)),
    status,
    retry,
    remove,
    progress.el,
  );
  list.append(item);

  const upload = {
    file,
    id: null,
    state: "uploading",
    el: item,
    status,
    request: null,
    sealed: null,
  };

  function fail(message, canRetry) {
    upload.state = "error";
    progress.hide();
    setText(status, message);
    retry.hidden = !canRetry;
    onChange?.(upload);
  }

  async function run() {
    upload.state = "uploading";
    setText(status, "");
    retry.hidden = true;
    progress.set(0);
    onChange?.(upload);
    let body = file;
    let name = file.name || "file";
    if (encrypt) {
      // Шифруем один раз: повтор загрузки отправит тот же шифротекст с тем же описанием
      if (!upload.sealed) {
        setText(status, "Шифруется…");
        try {
          upload.sealed = await encrypt(file);
        } catch {
          if (upload.state !== "removed") {
            fail("Не удалось зашифровать файл", true);
          }
          return;
        }
        if (upload.state === "removed") {
          return;
        }
        setText(status, "");
      }
      body = upload.sealed.body;
      name = undefined;
    }
    upload.request = uploadFile(url(), body, {
      name,
      headers: headers(),
      onProgress: (part) => progress.set(part),
    });
    upload.request.promise.then(
      (result) => {
        if (upload.state === "removed") {
          return;
        }
        upload.id = result.id;
        upload.state = "done";
        progress.hide();
        setText(status, "Загружен");
        onChange?.(upload);
        onDone?.(upload);
      },
      (error) => {
        if (upload.state !== "removed") {
          fail(error.render, true);
        }
      },
    );
  }

  retry.addEventListener("click", run);
  remove.addEventListener("click", () => {
    upload.state = "removed";
    upload.request?.abort();
    item.remove();
    onRemoved?.(upload);
  });

  const problem = fileProblem(file);
  if (problem) {
    fail(problem, false);
  } else {
    run();
  }
  return upload;
}

function bindFilePicker(button, input, onFiles) {
  button.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    const files = [...input.files];
    // Сбрасываем, чтобы тот же файл можно было выбрать ещё раз
    input.value = "";
    if (files.length) {
      onFiles(files);
    }
  });
}

function hasFiles(dataTransfer) {
  return Boolean(dataTransfer) && [...dataTransfer.types].includes("Files");
}

function acceptDroppedAndPasted(dropZone, pasteTarget, onFiles) {
  dropZone.addEventListener("dragover", (event) => {
    if (hasFiles(event.dataTransfer)) {
      event.preventDefault();
      dropZone.classList.add("dropping");
    }
  });
  dropZone.addEventListener("dragleave", (event) => {
    if (!dropZone.contains(event.relatedTarget)) {
      dropZone.classList.remove("dropping");
    }
  });
  dropZone.addEventListener("drop", (event) => {
    dropZone.classList.remove("dropping");
    if (hasFiles(event.dataTransfer)) {
      event.preventDefault();
      onFiles([...event.dataTransfer.files]);
    }
  });
  // Скриншот из буфера приходит файлом; обычный текст вставляется как раньше
  pasteTarget.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files ?? [])];
    if (files.length) {
      event.preventDefault();
      onFiles(files);
    }
  });
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = el("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

async function confirmReceived(url) {
  for (let attempt = 1; attempt <= CONFIRM_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(url, {
        method: "POST",
        signal: AbortSignal.timeout(CONFIRM_TIMEOUT_MS),
      });
      if (response.ok) {
        return;
      }
    } catch {
      // повторим ниже
    }
    await new Promise((resolve) => setTimeout(resolve, 1000 * attempt));
  }
  // Не вышло — не страшно: файл всё равно удалится, когда выйдет его срок
}

// Одноразовый файл качаем сами, а не обычной ссылкой: только так страница знает, что
// он дошёл целиком, и может подтвердить получение. Сервер этого знать не может.
// После обрыва повторный run() продолжает с того же байта (Range).
// Файл канала зашифрован: stored_size — размер шифротекста на сервере, decrypt проверяет и
// расшифровывает его. Получение подтверждаем только после успешной расшифровки
class OneTimeDownload {
  constructor(file, onProgress, decrypt = null) {
    this.file = file;
    this.onProgress = onProgress;
    this.decrypt = decrypt;
    this.size = file.stored_size ?? file.size;
    this.chunks = [];
    this.received = 0;
  }

  async run() {
    if (this.received < this.size) {
      await this.fetchRest();
    }
    let blob = new Blob(this.chunks, { type: this.file.content_type });
    if (this.decrypt) {
      try {
        blob = await this.decrypt(new Blob(this.chunks));
      } catch {
        this.chunks = [];
        this.received = 0;
        throw new FileDamagedError("Файл повреждён или подменён");
      }
    }
    saveBlob(blob, this.file.name);
    await confirmReceived(this.file.received_url);
  }

  async fetchRest() {
    // У fetch нет таймаута: зависшее соединение (данных нет, но и обрыва нет) ждало бы
    // вечно. Сторож прерывает его, если данные не приходят STALL_TIMEOUT_MS
    const controller = new AbortController();
    let watchdog;
    const rearm = () => {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => controller.abort(), STALL_TIMEOUT_MS);
    };
    rearm();
    try {
      const headers = this.received ? { Range: `bytes=${this.received}-` } : {};
      const response = await fetch(this.file.download_url, { headers, signal: controller.signal });
      if (response.status === 403 || response.status === 410) {
        throw new FileGoneError("Файл больше недоступен");
      }
      if (!response.ok) {
        throw new Error(`server error ${response.status}`);
      }
      if (response.status === 200 && this.received) {
        // Сервер прислал файл с начала — начинаем заново
        this.chunks = [];
        this.received = 0;
      }
      const reader = response.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        rearm();
        if (done) {
          break;
        }
        this.chunks.push(value);
        this.received += value.length;
        this.onProgress?.(this.received / this.size);
      }
    } finally {
      clearTimeout(watchdog);
    }
    if (this.received < this.size) {
      throw new Error("connection lost");
    }
  }
}

// Карточка файла: превью, имя, размер, скачивание.
// Обычный файл — простой ссылкой, одноразовый — с прогрессом и подтверждением получения.
// decrypt — для файла канала, см. OneTimeDownload
function createFileCard(file, { status = "", onReceived, decrypt } = {}) {
  const card = el("div", "file-card");
  if (file.preview_url) {
    const preview = el("img", "file-preview");
    preview.src = file.preview_url;
    preview.alt = file.name;
    preview.loading = "lazy";
    card.append(preview);
  }
  const statusEl = el("span", "file-status");
  setText(statusEl, status);
  const info = el("div", "file-info");
  info.append(
    el("span", "file-name", file.name),
    el("span", "file-size", () => formatSize(file.size)),
    statusEl,
  );
  const actions = el("div", "file-actions");
  const progress = createProgress();
  card.append(info, actions, progress.el);

  if (file.received_url) {
    const button = el("button", "button", msg("Скачать"));
    button.type = "button";
    const download = new OneTimeDownload(file, (part) => progress.set(part), decrypt);
    button.addEventListener("click", async () => {
      button.disabled = true;
      card.classList.remove("interrupted");
      setText(statusEl, "Скачивается…");
      try {
        await download.run();
        actions.replaceChildren();
        progress.hide();
        setText(statusEl, "Скачано");
        card.classList.add("received");
        onReceived?.();
      } catch (error) {
        button.disabled = false;
        if (error instanceof FileGoneError) {
          actions.replaceChildren();
          progress.hide();
          setText(statusEl, error.render);
          return;
        }
        if (error instanceof FileDamagedError) {
          progress.hide();
          setText(button, "Скачать заново");
          setText(statusEl, error.render);
          card.classList.add("interrupted");
          return;
        }
        setText(button, "Продолжить");
        setText(statusEl, "Связь оборвалась. Можно продолжить с того же места");
        card.classList.add("interrupted");
      }
    });
    actions.append(button);
  } else if (file.download_url) {
    const link = el("a", "button", msg("Скачать"));
    link.href = file.download_url;
    link.download = file.name;
    actions.append(link);
  }

  return {
    el: card,
    markReceived(text) {
      actions.replaceChildren();
      progress.hide();
      setText(statusEl, text);
      card.classList.add("received");
    },
  };
}

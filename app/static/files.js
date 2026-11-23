// Файлы — общее для заметок и канала: выбор, перетаскивание, вставка из буфера,
// загрузка с прогрессом и карточка файла со скачиванием.
// Использует showStatus из app.js — он нужен только во время работы.

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const CONFIRM_ATTEMPTS = 3;
const CONFIRM_TIMEOUT_MS = 10000;
// Столько ждём следующей порции данных, прежде чем считать соединение зависшим
const STALL_TIMEOUT_MS = 15000;

class FileGoneError extends Error {}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function formatSize(bytes) {
  if (bytes < 1024) {
    return `${bytes} Б`;
  }
  const units = ["КБ", "МБ", "ГБ"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value < 10 ? 1 : 0;
  return `${value.toLocaleString("ru-RU", { maximumFractionDigits: digits })} ${units[unit]}`;
}

function plural(count, one, few, many) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) {
    return one;
  }
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
    return few;
  }
  return many;
}

// Проверяем до загрузки, чтобы не гонять по сети то, что сервер всё равно отклонит
function fileProblem(file) {
  if (file.size === 0) {
    return "Пустой файл";
  }
  if (file.size > MAX_FILE_SIZE) {
    return `Больше ${formatSize(MAX_FILE_SIZE)}`;
  }
  return null;
}

function uploadErrorText(status) {
  switch (status) {
    case 400:
      return "Пустой файл";
    case 401:
    case 404:
      return "Канал недоступен";
    case 403:
      return "Загружать может только отправитель";
    case 413:
      return `Больше ${formatSize(MAX_FILE_SIZE)}`;
    case 429:
      return "Слишком много загрузок. Подождите минуту";
    case 507:
      return "Хранилище переполнено";
    default:
      return `Ошибка сервера (${status})`;
  }
}

// XMLHttpRequest, а не fetch: прогресс отправки есть только у него
function uploadFile(url, file, { headers = {}, onProgress } = {}) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open("POST", `${url}?name=${encodeURIComponent(file.name || "file")}`);
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
        reject(new Error(uploadErrorText(xhr.status)));
      }
    });
    xhr.addEventListener("error", () => reject(new Error("Нет связи с сервером")));
    xhr.addEventListener("abort", () => reject(new Error("Загрузка отменена")));
    xhr.send(file);
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
// onDone — когда файл загружен, onChange — при любой смене состояния
function startUpload(file, { url, headers = () => ({}), list, onDone, onChange, onRemoved }) {
  const item = el("li", "upload-item");
  const status = el("span", "file-status");
  const progress = createProgress();
  const retry = el("button", "button small", "Повторить");
  retry.type = "button";
  retry.hidden = true;
  const remove = el("button", "icon-button", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "Убрать файл");
  item.append(
    el("span", "file-name", file.name || "файл"),
    el("span", "file-size", formatSize(file.size)),
    status,
    retry,
    remove,
    progress.el,
  );
  list.append(item);

  const upload = { file, id: null, state: "uploading", el: item, status, request: null };

  function fail(message, canRetry) {
    upload.state = "error";
    progress.hide();
    status.textContent = message;
    retry.hidden = !canRetry;
    onChange?.(upload);
  }

  function run() {
    upload.state = "uploading";
    status.textContent = "";
    retry.hidden = true;
    progress.set(0);
    onChange?.(upload);
    upload.request = uploadFile(url(), file, {
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
        status.textContent = "Загружен";
        onChange?.(upload);
        onDone?.(upload);
      },
      (error) => {
        if (upload.state !== "removed") {
          fail(error.message, true);
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
// После обрыва повторный run() продолжает с того же байта (Range)
class OneTimeDownload {
  constructor(file, onProgress) {
    this.file = file;
    this.onProgress = onProgress;
    this.chunks = [];
    this.received = 0;
  }

  async run() {
    if (this.received < this.file.size) {
      await this.fetchRest();
    }
    saveBlob(new Blob(this.chunks, { type: this.file.content_type }), this.file.name);
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
        throw new Error(`Ошибка сервера (${response.status})`);
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
        this.onProgress?.(this.received / this.file.size);
      }
    } finally {
      clearTimeout(watchdog);
    }
    if (this.received < this.file.size) {
      throw new Error("Связь оборвалась");
    }
  }
}

// Карточка файла: превью, имя, размер, скачивание.
// Обычный файл — простой ссылкой, одноразовый — с прогрессом и подтверждением получения
function createFileCard(file, { status = "", onReceived } = {}) {
  const card = el("div", "file-card");
  if (file.preview_url) {
    const preview = el("img", "file-preview");
    preview.src = file.preview_url;
    preview.alt = file.name;
    preview.loading = "lazy";
    card.append(preview);
  }
  const statusEl = el("span", "file-status", status);
  const info = el("div", "file-info");
  info.append(el("span", "file-name", file.name), el("span", "file-size", formatSize(file.size)), statusEl);
  const actions = el("div", "file-actions");
  const progress = createProgress();
  card.append(info, actions, progress.el);

  if (file.received_url) {
    const button = el("button", "button", "Скачать");
    button.type = "button";
    const download = new OneTimeDownload(file, (part) => progress.set(part));
    button.addEventListener("click", async () => {
      button.disabled = true;
      card.classList.remove("interrupted");
      statusEl.textContent = "Скачивается…";
      try {
        await download.run();
        actions.replaceChildren();
        progress.hide();
        statusEl.textContent = "Скачано";
        card.classList.add("received");
        onReceived?.();
      } catch (error) {
        button.disabled = false;
        if (error instanceof FileGoneError) {
          actions.replaceChildren();
          progress.hide();
          statusEl.textContent = error.message;
          return;
        }
        button.textContent = "Продолжить";
        statusEl.textContent = "Связь оборвалась. Можно продолжить с того же места";
        card.classList.add("interrupted");
      }
    });
    actions.append(button);
  } else if (file.download_url) {
    const link = el("a", "button", "Скачать");
    link.href = file.download_url;
    link.download = file.name;
    actions.append(link);
  }

  return {
    el: card,
    markReceived(text) {
      actions.replaceChildren();
      progress.hide();
      statusEl.textContent = text;
      card.classList.add("received");
    },
  };
}

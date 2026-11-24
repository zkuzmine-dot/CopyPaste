// Ключи устройства для каналов — в IndexedDB: приватный ключ хранится там как CryptoKey и
// не извлекается. Запись по коду канала:
//   role        — "creator" или "joiner": от неё зависит порядок обмена ключами
//   privateKey  — неизвлекаемый CryptoKey; publicKey — его открытая часть (65 байт)
//   revealedTo  — отпечаток, в ответ на который создатель уже отправил ключ
//   peerKey     — ключ собеседника, с которым посчитан общий ключ
//   createdAt   — по нему чистятся записи давно закрытых каналов

const KEYSTORE_DB = "copypaste";
const KEYSTORE_STORE = "channel-keys";
const KEYSTORE_MAX_AGE = 7 * 24 * 60 * 60 * 1000;

// Без IndexedDB (запрещён в настройках браузера) ключи живут до перезагрузки вкладки
const keystoreMemory = new Map();

function openKeystore() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(KEYSTORE_DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(KEYSTORE_STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function inKeystore(mode, action) {
  const db = await openKeystore();
  try {
    return await new Promise((resolve, reject) => {
      const transaction = db.transaction(KEYSTORE_STORE, mode);
      const request = action(transaction.objectStore(KEYSTORE_STORE));
      transaction.oncomplete = () => resolve(request.result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  } finally {
    db.close();
  }
}

async function saveChannelKeys(slug, record) {
  keystoreMemory.set(slug, record);
  try {
    await inKeystore("readwrite", (store) => store.put(record, slug));
  } catch {
    // останется в памяти вкладки
  }
}

async function loadChannelKeys(slug) {
  if (keystoreMemory.has(slug)) {
    return keystoreMemory.get(slug);
  }
  try {
    const record = await inKeystore("readonly", (store) => store.get(slug));
    return record ?? null;
  } catch {
    return null;
  }
}

async function forgetChannelKeys(slug) {
  keystoreMemory.delete(slug);
  try {
    await inKeystore("readwrite", (store) => store.delete(slug));
  } catch {
    // нечего удалять
  }
}

// Канал закрывается через минуты бездействия, а запись остаётся, если вкладку закрыли раньше
async function pruneChannelKeys() {
  const oldest = Date.now() - KEYSTORE_MAX_AGE;
  try {
    await inKeystore("readwrite", (store) => {
      const request = store.openCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) {
          return;
        }
        if (cursor.value.createdAt < oldest) {
          cursor.delete();
        }
        cursor.continue();
      };
      return request;
    });
  } catch {
    // чистка не обязательна
  }
}

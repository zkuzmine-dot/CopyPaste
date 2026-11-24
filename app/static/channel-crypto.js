// Шифрование канала в браузере: ключи устройства, общий ключ и проверочные фигуры.
// Без DOM: те же функции проверяют тесты в Node (tests/js/). Порядок обмена ключами —
// в README, раздел «Обмен ключами».

const CHANNEL_CRYPTO_LABEL = "copypaste/channel/v1";
const DEVICE_KEY_ALGORITHM = { name: "ECDH", namedCurve: "P-256" };
const SHAPE_COUNT = 4;
const SHAPE_BITS = 5;
const MESSAGE_NONCE_BYTES = 12;
// Как CHANNEL_FILE_CHUNK_BYTES на сервере: по нему считается предел размера загрузки
const FILE_CHUNK_BYTES = 64 * 1024;
const FILE_TAG_BYTES = 16;
const FILE_NONCE_BYTES = 8;
const FILE_NAME_MAX_LENGTH = 255;

const textEncoder = new TextEncoder();

// WebCrypto есть только в защищённом контексте: на HTTPS или localhost
function cryptoAvailable() {
  return Boolean(globalThis.crypto?.subtle);
}

function toBase64Url(bytes) {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function fromBase64Url(text) {
  const base64 = text.replaceAll("-", "+").replaceAll("_", "/");
  const binary = atob(base64 + "=".repeat((4 - (base64.length % 4)) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function concatBytes(...parts) {
  const result = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

// Приватный ключ неизвлекаемый: страница может им пользоваться, но прочитать его байты не может
async function createDeviceKeys() {
  const pair = await crypto.subtle.generateKey(DEVICE_KEY_ALGORITHM, false, ["deriveBits"]);
  const publicKey = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  return { privateKey: pair.privateKey, publicKey };
}

async function keyCommitment(publicKey) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", publicKey));
}

async function matchesCommitment(publicKey, commitment) {
  const actual = await keyCommitment(publicKey);
  return actual.length === commitment.length && actual.every((byte, i) => byte === commitment[i]);
}

// Оба устройства передают ключи в одном порядке — создателя, потом подключившегося, — и
// получают одинаковые ключи и фигуры. Ключи привязаны к каналу и к обоим открытым ключам
async function deriveChannelCipher(privateKey, peerPublicKey, { slug, creatorKey, joinerKey }) {
  const peer = await crypto.subtle.importKey("raw", peerPublicKey, DEVICE_KEY_ALGORITHM, false, []);
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: peer }, privateKey, 256);
  const secret = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const salt = textEncoder.encode(slug);
  const keyFor = (purpose) =>
    crypto.subtle.deriveKey(
      {
        name: "HKDF",
        hash: "SHA-256",
        salt,
        info: concatBytes(
          textEncoder.encode(`${CHANNEL_CRYPTO_LABEL}/${purpose}`),
          creatorKey,
          joinerKey,
        ),
      },
      secret,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"],
    );
  const [textKey, fileKey, shapes] = await Promise.all([
    keyFor("text"),
    keyFor("file"),
    verificationShapes(creatorKey, joinerKey),
  ]);
  return { textKey, fileKey, shapes };
}

// Четыре фигуры по 5 бит: 16 форм, каждая залитая или контурная. Считаются из обоих
// открытых ключей, поэтому при подмене ключа на одном из устройств фигуры другие
async function verificationShapes(creatorKey, joinerKey) {
  const label = textEncoder.encode(`${CHANNEL_CRYPTO_LABEL}/shapes`);
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", concatBytes(label, creatorKey, joinerKey)),
  );
  const bits = (digest[0] << 16) | (digest[1] << 8) | digest[2];
  return Array.from({ length: SHAPE_COUNT }, (_, i) => {
    const value = (bits >> (24 - SHAPE_BITS * (i + 1))) & ((1 << SHAPE_BITS) - 1);
    return { shape: value >> 1, filled: (value & 1) === 0 };
  });
}

// Код канала и тип сообщения входят в AAD: шифротекст не перенести в другой канал и не выдать
// текст за описание файла
function messageAad(kind, slug) {
  return textEncoder.encode(`${CHANNEL_CRYPTO_LABEL}/${kind}/${slug}`);
}

// Сообщение — nonce (12 случайных байт), шифротекст и тег AES-GCM
async function sealMessage(key, kind, slug, plain) {
  const nonce = crypto.getRandomValues(new Uint8Array(MESSAGE_NONCE_BYTES));
  const sealed = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce, additionalData: messageAad(kind, slug) },
    key,
    plain,
  );
  return concatBytes(nonce, new Uint8Array(sealed));
}

// Испорченное, чужое или обрезанное сообщение не расшифруется: ошибка, а не мусор
async function openMessage(key, kind, slug, payload) {
  const plain = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: payload.subarray(0, MESSAGE_NONCE_BYTES),
      additionalData: messageAad(kind, slug),
    },
    key,
    payload.subarray(MESSAGE_NONCE_BYTES),
  );
  return new Uint8Array(plain);
}

function sealText(key, slug, text) {
  return sealMessage(key, "text", slug, textEncoder.encode(text));
}

async function openText(key, slug, payload) {
  const plain = await openMessage(key, "text", slug, payload);
  return new TextDecoder("utf-8", { fatal: true }).decode(plain);
}

function uint32(value) {
  const bytes = new Uint8Array(4);
  new DataView(bytes.buffer).setUint32(0, value);
  return bytes;
}

// Как у файлов заметок с паролем (app/utils/crypto.py): nonce куска — 8 случайных байт файла и
// номер куска, в AAD ещё признак последнего. Куски не переставить, не перенести в другой файл и
// не отрезать: файл, обрезанный ровно по границе куска, не сойдётся по тегу
function chunkParams(slug, fileNonce, index, last) {
  return {
    name: "AES-GCM",
    iv: concatBytes(fileNonce, uint32(index)),
    additionalData: concatBytes(
      textEncoder.encode(`${CHANNEL_CRYPTO_LABEL}/file-chunk/${slug}/`),
      fileNonce,
      uint32(index),
      Uint8Array.of(last ? 1 : 0),
    ),
  };
}

async function sealFile(key, slug, file) {
  const nonce = crypto.getRandomValues(new Uint8Array(FILE_NONCE_BYTES));
  const count = Math.max(1, Math.ceil(file.size / FILE_CHUNK_BYTES));
  const parts = [];
  for (let index = 0; index < count; index += 1) {
    const start = index * FILE_CHUNK_BYTES;
    const plain = await file.slice(start, start + FILE_CHUNK_BYTES).arrayBuffer();
    const params = chunkParams(slug, nonce, index, index === count - 1);
    parts.push(new Uint8Array(await crypto.subtle.encrypt(params, key, plain)));
  }
  return { body: new Blob(parts), nonce };
}

async function openFile(key, slug, nonce, sealed) {
  const stored = FILE_CHUNK_BYTES + FILE_TAG_BYTES;
  const count = Math.ceil(sealed.size / stored);
  if (count === 0) {
    throw new Error("Empty file");
  }
  const parts = [];
  for (let index = 0; index < count; index += 1) {
    const chunk = await sealed.slice(index * stored, (index + 1) * stored).arrayBuffer();
    const params = chunkParams(slug, nonce, index, index === count - 1);
    parts.push(new Uint8Array(await crypto.subtle.decrypt(params, key, chunk)));
  }
  return new Blob(parts);
}

// Описание файла — сообщение вида "file": имя, размер и nonce. Сервер видит только длину
function sealFileMeta(key, slug, { name, size, nonce }) {
  const meta = { name: name.slice(0, FILE_NAME_MAX_LENGTH) || "файл", size, nonce: toBase64Url(nonce) };
  return sealMessage(key, "file", slug, textEncoder.encode(JSON.stringify(meta)));
}

async function openFileMeta(key, slug, payload) {
  const plain = await openMessage(key, "file", slug, payload);
  const meta = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(plain));
  if (
    typeof meta?.name !== "string" ||
    !Number.isSafeInteger(meta.size) ||
    typeof meta.nonce !== "string"
  ) {
    throw new Error("Bad file description");
  }
  return { name: meta.name, size: meta.size, nonce: fromBase64Url(meta.nonce) };
}

if (typeof module !== "undefined") {
  module.exports = {
    cryptoAvailable,
    toBase64Url,
    fromBase64Url,
    createDeviceKeys,
    keyCommitment,
    matchesCommitment,
    deriveChannelCipher,
    verificationShapes,
    sealMessage,
    openMessage,
    sealText,
    openText,
    sealFile,
    openFile,
    sealFileMeta,
    openFileMeta,
  };
}

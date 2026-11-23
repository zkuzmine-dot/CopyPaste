// Шифрование канала в браузере: ключи устройства, общий ключ и проверочные фигуры.
// Без DOM: те же функции проверяют тесты в Node (tests/js/). Порядок обмена ключами —
// в README, раздел «Обмен ключами».

const CHANNEL_CRYPTO_LABEL = "copypaste/channel/v1";
const DEVICE_KEY_ALGORITHM = { name: "ECDH", namedCurve: "P-256" };
const SHAPE_COUNT = 4;
const SHAPE_BITS = 5;

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
  };
}

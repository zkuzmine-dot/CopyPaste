// make test-js — шифрование канала из app/static/channel-crypto.js

const test = require("node:test");
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");

const cc = require("../../app/static/channel-crypto.js");

const SLUG = "abcd2345";

async function exchange({ slug = SLUG, joinerSlug = slug } = {}) {
  const creator = await cc.createDeviceKeys();
  const joiner = await cc.createDeviceKeys();
  const order = { creatorKey: creator.publicKey, joinerKey: joiner.publicKey };
  return {
    creator: await cc.deriveChannelCipher(creator.privateKey, joiner.publicKey, { slug, ...order }),
    joiner: await cc.deriveChannelCipher(joiner.privateKey, creator.publicKey, {
      slug: joinerSlug,
      ...order,
    }),
  };
}

async function roundtrip(encryptKey, decryptKey) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const sealed = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    encryptKey,
    new TextEncoder().encode("привет"),
  );
  return new TextDecoder().decode(
    await crypto.subtle.decrypt({ name: "AES-GCM", iv }, decryptKey, sealed),
  );
}

// Независимо от модуля: первые 20 бит SHA-256, по 5 бит на фигуру, чётное значение — залитая
function expectedShapes(creatorKey, joinerKey) {
  const digest = createHash("sha256")
    .update("copypaste/channel/v1/shapes")
    .update(creatorKey)
    .update(joinerKey)
    .digest();
  const bits = digest.readUIntBE(0, 3) >>> 4;
  return [15, 10, 5, 0].map((shift) => {
    const value = (bits >>> shift) & 31;
    return { shape: Math.floor(value / 2), filled: value % 2 === 0 };
  });
}

test("WebCrypto доступен", () => {
  assert.equal(cc.cryptoAvailable(), true);
});

test("открытый ключ — несжатая точка P-256, приватный не извлекается", async () => {
  const keys = await cc.createDeviceKeys();

  assert.equal(keys.publicKey.length, 65);
  assert.equal(keys.publicKey[0], 0x04);
  assert.equal(keys.privateKey.extractable, false);
  await assert.rejects(crypto.subtle.exportKey("pkcs8", keys.privateKey));
});

test("отпечаток — SHA-256 открытого ключа, как считает сервер", async () => {
  const keys = await cc.createDeviceKeys();
  const other = await cc.createDeviceKeys();
  const commitment = await cc.keyCommitment(keys.publicKey);

  assert.deepEqual(Buffer.from(commitment), createHash("sha256").update(keys.publicKey).digest());
  assert.equal(await cc.matchesCommitment(keys.publicKey, commitment), true);
  assert.equal(await cc.matchesCommitment(other.publicKey, commitment), false);
});

test("base64url без «=» — тот же, что у Node и сервера", () => {
  for (const length of [0, 1, 2, 3, 32, 65]) {
    const bytes = Uint8Array.from({ length }, (_, i) => (i * 37 + 250) % 256);
    const text = cc.toBase64Url(bytes);

    assert.equal(text, Buffer.from(bytes).toString("base64url"));
    assert.deepEqual(cc.fromBase64Url(text), bytes);
  }
});

test("оба устройства получают одни и те же ключи и фигуры", async () => {
  const { creator, joiner } = await exchange();

  assert.equal(await roundtrip(creator.textKey, joiner.textKey), "привет");
  assert.equal(await roundtrip(joiner.fileKey, creator.fileKey), "привет");
  assert.deepEqual(creator.shapes, joiner.shapes);
});

test("общие ключи неизвлекаемы", async () => {
  const { creator } = await exchange();

  assert.equal(creator.textKey.extractable, false);
  assert.equal(creator.fileKey.extractable, false);
});

test("ключи для текста и для файлов разные", async () => {
  const { creator, joiner } = await exchange();

  await assert.rejects(roundtrip(creator.textKey, joiner.fileKey));
});

test("ключ привязан к каналу", async () => {
  const { creator, joiner } = await exchange({ joinerSlug: "zzzz2345" });

  await assert.rejects(roundtrip(creator.textKey, joiner.textKey));
});

test("при подмене ключей посередине общий ключ у устройств разный", async () => {
  const [creator, joiner, fakeJoiner, fakeCreator] = await Promise.all(
    Array.from({ length: 4 }, () => cc.createDeviceKeys()),
  );
  // Создатель говорит с подменой второго устройства, второе — с подменой создателя
  const creatorSide = await cc.deriveChannelCipher(creator.privateKey, fakeJoiner.publicKey, {
    slug: SLUG,
    creatorKey: creator.publicKey,
    joinerKey: fakeJoiner.publicKey,
  });
  const joinerSide = await cc.deriveChannelCipher(joiner.privateKey, fakeCreator.publicKey, {
    slug: SLUG,
    creatorKey: fakeCreator.publicKey,
    joinerKey: joiner.publicKey,
  });

  await assert.rejects(roundtrip(creatorSide.textKey, joinerSide.textKey));
});

test("фигуры считаются из обоих ключей по 5 бит", async () => {
  const creatorKey = Uint8Array.from({ length: 65 }, (_, i) => i);
  const joinerKey = Uint8Array.from({ length: 65 }, (_, i) => 255 - i);

  const shapes = await cc.verificationShapes(creatorKey, joinerKey);

  assert.deepEqual(shapes, expectedShapes(creatorKey, joinerKey));
  for (const { shape, filled } of shapes) {
    assert.ok(Number.isInteger(shape) && shape >= 0 && shape < 16);
    assert.equal(typeof filled, "boolean");
  }
});

test("подменённый ключ меняет фигуры", async () => {
  const creatorKey = Uint8Array.from({ length: 65 }, (_, i) => i);
  const joinerKey = Uint8Array.from({ length: 65 }, (_, i) => 255 - i);
  const fakeKey = Uint8Array.from({ length: 65 }, (_, i) => (i * 7) % 256);

  const real = await cc.verificationShapes(creatorKey, joinerKey);

  assert.notDeepEqual(await cc.verificationShapes(creatorKey, fakeKey), real);
  assert.notDeepEqual(await cc.verificationShapes(fakeKey, joinerKey), real);
  // Порядок ролей важен: иначе устройства с переставленными ключами совпали бы
  assert.notDeepEqual(await cc.verificationShapes(joinerKey, creatorKey), real);
});

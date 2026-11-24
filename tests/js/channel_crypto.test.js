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

// Как на сервере: MESSAGE_PAYLOAD_MAX_BYTES в app/schemas/channel.py
const TEXT_MAX_UNITS = 100000;
const PAYLOAD_MAX_BYTES = 12 + 3 * TEXT_MAX_UNITS + 16;

test("текст, зашифрованный одним устройством, читает другое", async () => {
  const { creator, joiner } = await exchange();

  const payload = await cc.sealText(creator.textKey, SLUG, "код домофона: 4521");

  assert.equal(await cc.openText(joiner.textKey, SLUG, payload), "код домофона: 4521");
  assert.equal(payload.length, 12 + Buffer.byteLength("код домофона: 4521") + 16);
  assert.equal(Buffer.from(payload).includes(Buffer.from("4521")), false);
});

test("одинаковый текст каждый раз шифруется по-разному", async () => {
  const { creator } = await exchange();

  const first = await cc.sealText(creator.textKey, SLUG, "привет");
  const second = await cc.sealText(creator.textKey, SLUG, "привет");

  assert.notDeepEqual(first, second);
});

test("самый длинный текст укладывается в лимит сервера", async () => {
  const { creator, joiner } = await exchange();
  // 3 байта в UTF-8 на знак UTF-16 — худший случай; эмодзи — 4 байта, но на 2 знака
  const widest = "€".repeat(TEXT_MAX_UNITS);
  const emoji = "😀".repeat(TEXT_MAX_UNITS / 2);

  const payload = await cc.sealText(creator.textKey, SLUG, widest);

  assert.equal(payload.length, PAYLOAD_MAX_BYTES);
  assert.equal(await cc.openText(joiner.textKey, SLUG, payload), widest);
  assert.ok((await cc.sealText(creator.textKey, SLUG, emoji)).length < PAYLOAD_MAX_BYTES);
});

test("испорченное, обрезанное или чужое сообщение не расшифровывается", async () => {
  const { creator, joiner } = await exchange();
  const payload = await cc.sealText(creator.textKey, SLUG, "привет");
  const flipped = payload.slice();
  flipped[20] ^= 1;

  await assert.rejects(cc.openText(joiner.textKey, SLUG, flipped));
  await assert.rejects(cc.openText(joiner.textKey, SLUG, payload.slice(0, -1)));
  await assert.rejects(cc.openText(joiner.textKey, "zzzz2345", payload));
  await assert.rejects(cc.openText(joiner.fileKey, SLUG, payload));
  await assert.rejects(cc.openMessage(joiner.textKey, "file", SLUG, payload));
});

const CHUNK = 64 * 1024;
const STORED_CHUNK = CHUNK + 16;

function randomBlob(size) {
  const bytes = new Uint8Array(size);
  for (let offset = 0; offset < size; offset += 65536) {
    crypto.getRandomValues(bytes.subarray(offset, Math.min(offset + 65536, size)));
  }
  return { blob: new Blob([bytes]), bytes };
}

async function bytesOf(blob) {
  return new Uint8Array(await blob.arrayBuffer());
}

test("файл: куски по 64 КБ, у каждого тег; второе устройство расшифровывает", async () => {
  const { creator, joiner } = await exchange();
  for (const size of [1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 5]) {
    const { blob, bytes } = randomBlob(size);

    const { body, nonce } = await cc.sealFile(creator.fileKey, SLUG, blob);

    assert.equal(nonce.length, 8);
    assert.equal(body.size, size + 16 * Math.ceil(size / CHUNK));
    assert.deepEqual(await bytesOf(await cc.openFile(joiner.fileKey, SLUG, nonce, body)), bytes);
  }
});

test("файл: подмена, перестановка, обрезка и чужой ключ не проходят", async () => {
  const { creator, joiner } = await exchange();
  const { blob } = randomBlob(3 * CHUNK);
  const { body, nonce } = await cc.sealFile(creator.fileKey, SLUG, blob);
  const sealed = await bytesOf(body);
  const open = (data, options = {}) =>
    cc.openFile(options.key ?? joiner.fileKey, options.slug ?? SLUG, options.nonce ?? nonce, new Blob([data]));

  const flipped = sealed.slice();
  flipped[CHUNK + 100] ^= 1;
  const swapped = new Uint8Array(sealed.length);
  swapped.set(sealed.subarray(STORED_CHUNK, 2 * STORED_CHUNK), 0);
  swapped.set(sealed.subarray(0, STORED_CHUNK), STORED_CHUNK);
  swapped.set(sealed.subarray(2 * STORED_CHUNK), 2 * STORED_CHUNK);
  const otherNonce = nonce.slice();
  otherNonce[0] ^= 1;

  await assert.rejects(open(flipped), "подменённый байт");
  await assert.rejects(open(swapped), "переставленные куски");
  // Без последнего куска предпоследний выглядит последним, но зашифрован не как последний
  await assert.rejects(open(sealed.subarray(0, 2 * STORED_CHUNK)), "обрезка по границе куска");
  await assert.rejects(open(sealed.subarray(0, sealed.length - 1)), "без последнего байта");
  await assert.rejects(open(new Uint8Array(0)), "пустой");
  await assert.rejects(open(sealed, { nonce: otherNonce }), "чужой nonce");
  await assert.rejects(open(sealed, { slug: "zzzz2345" }), "другой канал");
  await assert.rejects(open(sealed, { key: joiner.textKey }), "ключ для текста");
});

test("описание файла: имя, размер и nonce доходят, имя обрезается до 255 знаков", async () => {
  const { creator, joiner } = await exchange();
  const nonce = Uint8Array.from({ length: 8 }, (_, i) => i);

  const payload = await cc.sealFileMeta(creator.textKey, SLUG, {
    name: "отчёт.pdf",
    size: 12345,
    nonce,
  });
  const long = await cc.sealFileMeta(creator.textKey, SLUG, { name: "я".repeat(300), size: 1, nonce });

  assert.deepEqual(await cc.openFileMeta(joiner.textKey, SLUG, payload), {
    name: "отчёт.pdf",
    size: 12345,
    nonce,
  });
  assert.equal((await cc.openFileMeta(joiner.textKey, SLUG, long)).name.length, 255);
  assert.equal(Buffer.from(payload).includes(Buffer.from("отчёт")), false);
});

test("описание файла укладывается в лимит сервера даже с худшим именем", async () => {
  const { creator } = await exchange();
  // Управляющие знаки JSON экранирует как \\uXXXX — по 6 байт на знак
  const worst = "\u0001".repeat(300);

  const payload = await cc.sealFileMeta(creator.textKey, SLUG, {
    name: worst,
    size: Number.MAX_SAFE_INTEGER,
    nonce: new Uint8Array(8),
  });

  // FILE_META_PAYLOAD_MAX_BYTES в app/schemas/channel.py
  assert.ok(payload.length <= 12 + 2048 + 16, String(payload.length));
});

test("описание файла не выдать за текст и наоборот", async () => {
  const { creator, joiner } = await exchange();
  const meta = await cc.sealFileMeta(creator.textKey, SLUG, {
    name: "a.txt",
    size: 1,
    nonce: new Uint8Array(8),
  });
  const text = await cc.sealText(creator.textKey, SLUG, '{"name":"a.txt","size":1,"nonce":"AAAAAAAAAAA"}');

  await assert.rejects(cc.openText(joiner.textKey, SLUG, meta));
  await assert.rejects(cc.openFileMeta(joiner.textKey, SLUG, text));
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

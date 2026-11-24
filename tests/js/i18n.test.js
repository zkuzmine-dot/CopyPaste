// Английский словарь сверяется с исходниками: у каждой русской строки интерфейса есть
// перевод с теми же {параметрами}, а лишних записей нет
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const STATIC = path.join(__dirname, "../../app/static");
const { EN_MESSAGES } = require(path.join(STATIC, "i18n-en.js"));
globalThis.EN_MESSAGES = EN_MESSAGES;
const { fill, translateTo, CYRILLIC } = require(path.join(STATIC, "i18n.js"));

const CODE_FILES = ["app.js", "channel.js", "files.js"];
const TRANSLATED_ATTRS = ["placeholder", "aria-label", "title", "alt"];

function codeWithoutComments(file) {
  return fs
    .readFileSync(path.join(STATIC, file), "utf8")
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n");
}

function codeStrings() {
  const found = [];
  for (const file of CODE_FILES) {
    for (const [, text] of codeWithoutComments(file).matchAll(/"((?:[^"\\\n]|\\.)*)"/g)) {
      if (CYRILLIC.test(text)) {
        found.push(text);
      }
    }
  }
  return found;
}

// Так же, как i18n.js находит статичный текст: текстовые узлы и подписи
function pageStrings() {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const found = [];
  for (const [, raw] of html.matchAll(/>([^<]+)</g)) {
    const text = raw.replace(/\s+/g, " ").trim();
    if (CYRILLIC.test(text)) {
      found.push(text);
    }
  }
  const attrs = new RegExp(`\\s(?:${TRANSLATED_ATTRS.join("|")})="([^"]*)"`, "g");
  for (const [, text] of html.matchAll(attrs)) {
    if (CYRILLIC.test(text)) {
      found.push(text);
    }
  }
  return found;
}

function placeholders(template) {
  return [...template.matchAll(/\{(\w+)(?::([^}]*))?\}/g)]
    .map(([, name, forms]) => (forms === undefined ? name : `${name}:plural`))
    .sort();
}

function pluralForms(template) {
  return [...template.matchAll(/\{\w+:([^}]*)\}/g)].map(([, forms]) => forms.split("|").length);
}

const sources = new Set([...codeStrings(), ...pageStrings()]);

test("every Russian string of the site has an English translation", () => {
  const missing = [...sources].filter((text) => !(text in EN_MESSAGES));
  assert.deepEqual(missing, []);
});

test("the dictionary has no entries the site does not use", () => {
  const unused = Object.keys(EN_MESSAGES).filter((text) => !sources.has(text));
  assert.deepEqual(unused, []);
});

test("translations keep the same parameters and plural forms", () => {
  for (const [ru, en] of Object.entries(EN_MESSAGES)) {
    assert.deepEqual(placeholders(en), placeholders(ru), ru);
    assert.ok(pluralForms(ru).every((count) => count === 3), `ru needs one|few|many: ${ru}`);
    assert.ok(pluralForms(en).every((count) => count === 2), `en needs one|other: ${en}`);
  }
});

test("English texts are not empty and contain no Cyrillic", () => {
  for (const [ru, en] of Object.entries(EN_MESSAGES)) {
    assert.ok(en.trim() && !CYRILLIC.test(en), ru);
  }
});

test("Russian text in code goes through t() with {params}, not template literals", () => {
  for (const file of CODE_FILES) {
    const literals = [...codeWithoutComments(file).matchAll(/`[^`]*`/g)].map(([text]) => text);
    assert.deepEqual(literals.filter((text) => CYRILLIC.test(text)), [], file);
  }
});

test("Russian plurals pick one, few and many", () => {
  const forms = "{n} {n:файл|файла|файлов}";
  const words = [1, 2, 4, 5, 11, 12, 21, 22, 25, 101].map((n) => fill(forms, { n }, "ru"));
  assert.deepEqual(words, [
    "1 файл", "2 файла", "4 файла", "5 файлов", "11 файлов",
    "12 файлов", "21 файл", "22 файла", "25 файлов", "101 файл",
  ]);
});

test("English plurals pick one and other", () => {
  const text = "В заметке {count} {count:файл|файла|файлов}. Они откроются вместе с текстом.";
  assert.match(translateTo("en", text, { count: 1 }), /\b1 file\b/);
  assert.match(translateTo("en", text, { count: 2 }), /\b2 files\b/);
  assert.match(translateTo("en", text, { count: 21 }), /\b21 files\b/);
});

test("numbers follow the language, functions are called at render time", () => {
  assert.equal(fill("{count} / {max}", { count: 0, max: 100000 }, "ru"), "0 / 100 000");
  assert.equal(fill("{count} / {max}", { count: 0, max: 100000 }, "en"), "0 / 100,000");
  let calls = 0;
  const params = { size: () => `${++calls} KB` };
  assert.equal(fill("{size}", params, "en"), "1 KB");
  assert.equal(fill("{size}", params, "en"), "2 KB");
});

test("unknown text stays Russian, unknown parameters stay as written", () => {
  assert.equal(translateTo("en", "Нет такой строки"), "Нет такой строки");
  assert.equal(translateTo("ru", "Ошибка {code}"), "Ошибка {code}");
  assert.equal(translateTo("en", "Канал не найден"), EN_MESSAGES["Канал не найден"]);
});

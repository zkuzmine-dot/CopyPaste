// Язык интерфейса. Исходный текст — русский, он же ключ английского словаря (i18n-en.js).
// Статичный текст страницы находится сам. Текст из кода ставится через setText и setAttr:
// они запоминают, как его нарисовать, и при смене языка всё перерисовывается на месте —
// без перезагрузки, которая стёрла бы черновик или уже прочитанную одноразовую заметку.
// Язык ставит prefs.js в <html lang> до отрисовки

const LOCALES = { ru: "ru-RU", en: "en-GB" };
// Порядок форм в {n:форма|форма|…} — категории Intl.PluralRules этого языка
const PLURAL_ORDER = { ru: ["one", "few", "many"], en: ["one", "other"] };
const TRANSLATED_ATTRS = ["placeholder", "aria-label", "title", "alt"];
const CYRILLIC = /[А-Яа-яЁё]/;

let language = "ru";

function currentLocale() {
  return LOCALES[language];
}

function fill(template, params, lang) {
  return template.replace(/\{(\w+)(?::([^}]*))?\}/g, (whole, name, forms) => {
    if (!(name in params)) {
      return whole;
    }
    const value = params[name];
    if (forms !== undefined) {
      const list = forms.split("|");
      const category = new Intl.PluralRules(LOCALES[lang]).select(value);
      const index = PLURAL_ORDER[lang].indexOf(category);
      return list[index === -1 ? list.length - 1 : index];
    }
    if (typeof value === "function") {
      return value();
    }
    if (typeof value === "number") {
      return value.toLocaleString(LOCALES[lang]);
    }
    return String(value);
  });
}

function translateTo(lang, text, params = {}) {
  const template = lang === "en" ? (EN_MESSAGES[text] ?? text) : text;
  return fill(template, params, lang);
}

function t(text, params = {}) {
  return translateTo(language, text, params);
}

// Текст, который нарисуется на языке, выбранном к моменту показа
function msg(text, params = {}) {
  return () => t(text, params);
}

// Ошибка с текстом для интерфейса: message — для логов, render — для экрана
class UiError extends Error {
  constructor(text, params = {}) {
    super(text);
    this.render = msg(text, params);
  }
}

const renders = new WeakMap();

function bind(node, attr, render) {
  let map = renders.get(node);
  if (!map) {
    map = new Map();
    renders.set(node, map);
  }
  map.set(attr, render);
  node.setAttribute("data-i18n", "");
  draw(node, attr, render);
}

function draw(node, attr, render) {
  if (attr) {
    node.setAttribute(attr, render());
  } else {
    node.textContent = render();
  }
}

// text — русский исходник с {параметрами} или готовая функция из msg()
function setText(node, text, params = {}) {
  bind(node, "", typeof text === "function" ? text : msg(text, params));
}

function setAttr(node, attr, text, params = {}) {
  bind(node, attr, typeof text === "function" ? text : msg(text, params));
}

// Пользовательский текст и имена файлов не переводятся: старая привязка не должна их затереть
function setPlainText(node, value) {
  renders.get(node)?.delete("");
  node.textContent = value;
}

const staticTexts = [];
const staticAttrs = [];

function isTranslatable(node) {
  return !node.closest('script, style, [translate="no"]');
}

function collectStatic(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const source = node.data.replace(/\s+/g, " ").trim();
    if (CYRILLIC.test(source) && isTranslatable(node.parentElement)) {
      const [, before, after] = node.data.match(/^(\s*)[\s\S]*?(\s*)$/);
      staticTexts.push({ node, source, before, after });
    }
  }
  const selector = TRANSLATED_ATTRS.map((attr) => `[${attr}]`).join(",");
  for (const el of root.querySelectorAll(selector)) {
    if (!isTranslatable(el)) {
      continue;
    }
    for (const attr of TRANSLATED_ATTRS) {
      const source = el.getAttribute(attr);
      if (source && CYRILLIC.test(source)) {
        staticAttrs.push({ el, attr, source });
      }
    }
  }
}

function applyLanguage() {
  for (const item of staticTexts) {
    item.node.data = item.before + t(item.source) + item.after;
  }
  for (const item of staticAttrs) {
    item.el.setAttribute(item.attr, t(item.source));
  }
  // Привязки читают и статичный текст (подпись выбранного срока), поэтому идут после него
  for (const node of document.querySelectorAll("[data-i18n]")) {
    for (const [attr, render] of renders.get(node) ?? []) {
      draw(node, attr, render);
    }
  }
  showLanguageToggle();
}

// Кнопка подписана языком, на который переключает, — на нём же её читает диктор
function showLanguageToggle() {
  const toggle = document.getElementById("lang-toggle");
  const target = language === "ru" ? "en" : "ru";
  toggle.textContent = target.toUpperCase();
  toggle.lang = target;
  toggle.setAttribute("aria-label", target === "en" ? "English" : "Русский");
}

function setLanguage(next) {
  language = next;
  document.documentElement.lang = next;
  try {
    localStorage.setItem(LANGUAGE_KEY, next);
  } catch {
    // без localStorage язык продержится до перезагрузки
  }
  applyLanguage();
}

if (typeof document !== "undefined") {
  language = document.documentElement.lang === "en" ? "en" : "ru";
  collectStatic(document.body);
  // Страница написана по-русски: переводить при загрузке нужно только на английский
  if (language === "ru") {
    showLanguageToggle();
  } else {
    applyLanguage();
  }
  document.documentElement.dataset.translated = "";
  document.getElementById("lang-toggle").addEventListener("click", () => {
    setLanguage(language === "ru" ? "en" : "ru");
  });
}

if (typeof module !== "undefined") {
  module.exports = { fill, translateTo, CYRILLIC };
}

// Тема и язык страницы. Файл грузится в <head> до стилей: оба должны стоять до первой
// отрисовки, иначе страница мигнёт не тем цветом или не тем языком.
// Пока пользователь не выбрал тему сам, она следует за системой. Язык без выбора — русский

const THEME_KEY = "copypaste:theme";
const LANGUAGE_KEY = "copypaste:lang";

function savedLanguage() {
  try {
    return localStorage.getItem(LANGUAGE_KEY) === "en" ? "en" : "ru";
  } catch {
    return "ru";
  }
}

document.documentElement.lang = savedLanguage();
const systemDark = matchMedia("(prefers-color-scheme: dark)");

function savedTheme() {
  try {
    const theme = localStorage.getItem(THEME_KEY);
    return theme === "light" || theme === "dark" ? theme : null;
  } catch {
    return null;
  }
}

function preferredTheme() {
  return savedTheme() ?? (systemDark.matches ? "dark" : "light");
}

document.documentElement.dataset.theme = preferredTheme();

// Полуобороты неба на кнопке только прибавляются: день всегда идёт вперёд, а не назад
let themeTurns = document.documentElement.dataset.theme === "dark" ? 1 : 0;

function showThemeState() {
  const dark = document.documentElement.dataset.theme === "dark";
  document.getElementById("theme-toggle")?.setAttribute("aria-pressed", String(dark));
}

function setTheme(theme) {
  const root = document.documentElement;
  if (root.dataset.theme === theme) {
    return;
  }
  root.dataset.theme = theme;
  themeTurns += 1;
  const dial = document.querySelector(".theme-dial");
  if (dial) {
    dial.style.transform = `rotate(${themeTurns * 180}deg)`;
  }
  showThemeState();
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    // без localStorage тема продержится до перезагрузки
  }
  setTheme(next);
}

systemDark.addEventListener("change", () => setTheme(preferredTheme()));

document.addEventListener("DOMContentLoaded", () => {
  showThemeState();
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
});

export type Theme = "day" | "night";

const KEY = "operdir_theme";

export function getTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "night" ? "night" : "day";
  } catch {
    return "day";
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* хранилище недоступно: тема просто не запомнится */
  }
}

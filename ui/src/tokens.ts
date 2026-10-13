// SPDX-License-Identifier: AGPL-3.0-only
// Theme tokens (study §2.5), from the palette of progress_page.html; no
// dependency, so that the build writes `ameesh-ui.css` with this same code.
import tokens from "./tokens.json";

export const FONT = tokens.other.font;
export const RADIUS = tokens.other.radius;

type Colors = Record<string, string[]>;
const COLORS = tokens.colors as Colors;

/** `--ameesh-color-x: <value>;` for one theme (0 light, 1 dark). */
export function publicTokens(theme: 0 | 1): string {
  return Object.entries(COLORS).map(([name, values]) =>
    `--ameesh-color-${name}:${values[theme]};`).join("");
}

/** The tokens alone, for a host page (`ameesh-ui.css`). */
export function tokensSheet(): string {
  const other = `--ameesh-font:${FONT};--ameesh-radius:${RADIUS};`;
  return `:root{${publicTokens(0)}${other}}`
    + `@media (prefers-color-scheme:dark){:root:not([data-ameesh-theme=light]){${publicTokens(1)}}}`
    + `:root[data-ameesh-theme=dark]{${publicTokens(1)}}`;
}

/** Private `--_x` of a component: the host's token, else the default. */
export function privateTokens(theme: 0 | 1): string {
  return Object.entries(COLORS).map(([name, values]) =>
    `--_${name}:var(--ameesh-color-${name},${values[theme]});`).join("");
}


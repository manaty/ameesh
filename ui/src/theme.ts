// SPDX-License-Identifier: AGPL-3.0-only
// Theme (study §2.5): public tokens `--ameesh-color-<name>`, `--ameesh-font`,
// `--ameesh-radius`, from the palette of progress_page.html. A host sets
// them to theme the components; otherwise each component falls back on the
// light or dark default, after its `theme` attribute (auto: the system's).
import { css, unsafeCSS, type CSSResult } from "lit";
import { FONT, RADIUS, privateTokens } from "./tokens";

const light = privateTokens(0);
const dark = privateTokens(1);

/** Shared base of every component: tokens, font, focus, reduced motion. */
export const base: CSSResult = css`
  :host {
    ${unsafeCSS(light)}
    --_font: var(--ameesh-font, ${unsafeCSS(FONT)});
    --_radius: var(--ameesh-radius, ${unsafeCSS(RADIUS)});
    color: var(--_ink);
    font-family: var(--_font);
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :host(:not([theme="light"])) { ${unsafeCSS(dark)} color-scheme: dark; }
  }
  :host([theme="dark"]) { ${unsafeCSS(dark)} color-scheme: dark; }
  :host([hidden]) { display: none; }
  *, *::before, *::after { box-sizing: border-box; }
  :focus-visible { outline: 2px solid var(--_focus); outline-offset: 2px; }
  button, input, select {
    font: inherit;
    color: var(--_ink);
    background: var(--_surface);
    border: 1px solid var(--_ink-2);
    border-radius: calc(var(--_radius) - 2px);
    padding: 4px 10px;
  }
  button { cursor: pointer; }
  button:disabled { cursor: default; color: var(--_muted); }
  .muted { color: var(--_muted); }
  .critical { color: var(--_critical); }
  @media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
  }
`;

// SPDX-License-Identifier: AGPL-3.0-only
// fr and en catalogues (study §5): the API returns stable codes, the
// components translate them; texts written by humans or agents are never
// translated. Plurals through Intl.PluralRules, dates and amounts through Intl.
import en from "./en.json";
import fr from "./fr.json";

export type Message = string | Partial<Record<Intl.LDMLPluralRule, string>>;
export type Catalogue = Record<string, Message>;
export const CATALOGUES: Record<string, Catalogue> = { en, fr };
export const LANGUAGES = Object.keys(CATALOGUES);
export const DEFAULT_LANGUAGE = "en";

/** First supported language among `candidates` (`fr-CA` → `fr`). */
export function pick(...candidates: (string | null | undefined)[]): string {
  for (const candidate of candidates) {
    const primary = (candidate ?? "").trim().toLowerCase().split(/[-_]/)[0];
    if (primary && primary in CATALOGUES) return primary;
  }
  return DEFAULT_LANGUAGE;
}

/** Language of an element: its `lang` (or an ancestor's, across shadow roots),
 * otherwise the document's; `fallback` (e.g. `GET /me`) after that. */
export function languageOf(element: Element, fallback?: string | null): string {
  let node: Node | null = element;
  while (node) {
    if (node instanceof Element && node.getAttribute("lang")) {
      return pick(node.getAttribute("lang"), fallback);
    }
    node = node.parentNode ?? (node instanceof ShadowRoot ? node.host : null);
  }
  return pick(fallback);
}

export type Params = Record<string, string | number>;

/** The message `key` in `lang`, `{name}` replaced by `params` (as text). */
export function translate(lang: string, key: string, params: Params = {}): string {
  const message = CATALOGUES[lang]?.[key] ?? CATALOGUES[DEFAULT_LANGUAGE][key];
  if (message === undefined) return key;
  let text: string;
  if (typeof message === "string") {
    text = message;
  } else {
    const n = Number(params.n ?? 0);
    text = message[new Intl.PluralRules(lang).select(n)] ?? message.other ?? key;
  }
  return text.replace(/\{(\w+)\}/g, (all, name: string) =>
    name in params ? formatValue(lang, params[name]) : all);
}

function formatValue(lang: string, value: string | number): string {
  return typeof value === "number" ? new Intl.NumberFormat(lang).format(value) : value;
}

export function formatDate(lang: string, ts: number): string {
  return new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" })
    .format(new Date(ts * 1000));
}

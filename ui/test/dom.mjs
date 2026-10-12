// SPDX-License-Identifier: AGPL-3.0-only
// A jsdom window as the global scope, then the built module (.test-build),
// as a page would load it. axe-core runs inside that window.
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html lang=\"en\"><head><title>t</title></head><body></body></html>",
  { url: "http://127.0.0.1:8780/ui/", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
for (const name of ["window", "document", "customElements", "HTMLElement", "Element", "Node",
  "ShadowRoot", "Document", "DocumentFragment", "MutationObserver", "CustomEvent", "Event",
  "SubmitEvent", "HTMLInputElement", "location", "CSSStyleSheet", "NodeFilter", "Comment",
  "Text", "HTMLTemplateElement", "requestAnimationFrame"]) {
  if (window[name] !== undefined) {
    Object.defineProperty(globalThis, name, { value: window[name], configurable: true, writable: true });
  }
}

export const ui = await import(new URL("../.test-build/ameesh-ui.js", import.meta.url));
export { window };

const require = createRequire(import.meta.url);
window.eval(await readFile(require.resolve("axe-core/axe.min.js"), "utf8"));

/** axe-core on the document; contrast is checked on the tokens (jsdom has no layout). */
export async function violations(root = window.document) {
  const result = await window.axe.run(root, { rules: { "color-contrast": { enabled: false } } });
  // an array of this realm (axe's is the window's)
  return Array.from(result.violations, (v) => `${v.id}: ${Array.from(v.nodes, (n) => n.target).join(" ")}`);
}

/** A fake fetch: `routes` maps a path to [status, body]; calls are recorded. */
export function fakeFetch(routes) {
  const calls = [];
  const fn = async (url, init = {}) => {
    const path = new URL(url).pathname + new URL(url).search;
    calls.push({ path, init, headers: new Headers(init.headers) });
    const [status, body] = routes[path] ?? [404, { schema: "ameesh-error/1", error: "not_found", message: "no" }];
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  };
  fn.calls = calls;
  return fn;
}

export const settle = async (el) => {
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setTimeout(r, 0));
    if (el?.updateComplete) await el.updateComplete;
  }
};

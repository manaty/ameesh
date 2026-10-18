// SPDX-License-Identifier: AGPL-3.0-only
// Mounts the components on the example page (L92). It takes the module as an
// argument so that the page (app.mjs) passes the module served at
// /ui/ameesh-ui.js, while the proof passes the same module built by npm test:
// one wiring, exercised by both.
import { createHost } from "./host.mjs";
import { MESH_ID } from "./fixtures.mjs";

/**
 * @param {object} ui  the @ameesh/ui module (connectHost)
 * @param {object} [options]
 * @param {Window} [options.window]  the page, default: the global one
 * @param {string} [options.apiBase] mesh base, default: the page's origin
 * @param {string} [options.token]   token handed to the components
 * @param {ParentNode} [options.root] where <ameesh-config> lives
 * @param {(text: string) => void} [options.status] one line for the human
 */
export function mount(ui, options = {}) {
  const window = options.window ?? globalThis.window;
  const apiBase = options.apiBase ?? window.location.origin;
  const root = options.root ?? window.document;
  const host = createHost({ window, apiBase, token: options.token, status: options.status });
  const off = ui.connectHost(host, { root, mesh: MESH_ID, apiBase });
  return { host, config: window.document.querySelector("ameesh-config"), off };
}

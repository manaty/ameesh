// SPDX-License-Identifier: AGPL-3.0-only
// The example page's script (L92): it binds the host object to the components
// and wires the demo toolbar. The interface is the module `ameesh serve`
// serves at /ui/ameesh-ui.js; an application embedding the npm package
// imports the same names from "@ameesh/ui" (GUIDE-PAGE-WEB.md).
import * as ui from "/ui/ameesh-ui.js";
import { mount } from "./mount.mjs";

const at = (id) => document.getElementById(id);
const status = (text) => { at("status").textContent = text; };

const { host, config } = mount(ui, { window, status });
void host.meshes().then((list) => status(`embedded: ${list[0]?.name ?? "?"} — ${location.origin}`));

// Theme and language are the host's: it sets them on <ameesh-config>, the
// components never touch the host page (CONTRAT-HOTE.md). The host chrome
// follows through the published tokens (`data-ameesh-theme` on :root).
at("theme").addEventListener("change", (event) => {
  config.theme = event.target.value;
  if (event.target.value === "auto") delete document.documentElement.dataset.ameeshTheme;
  else document.documentElement.dataset.ameeshTheme = event.target.value;
});
at("language").addEventListener("change", (event) => { config.lang = event.target.value; });

// L90 will raise these two events from its screens; here the host raises them
// itself, to show where they go: a notification and a foreground approval.
at("notify").addEventListener("click", () => {
  config.dispatchEvent(new CustomEvent("ameesh:alert", { bubbles: true, composed: true,
    detail: { kind: "demo", title: "Acme", text: "The panier lot is waiting for a verdict." } }));
});
at("approval").addEventListener("click", () => {
  config.dispatchEvent(new CustomEvent("ameesh:approval-requested", { bubbles: true, composed: true,
    detail: { url: new URL("/a/demo", location.origin).href } }));
});

// SPDX-License-Identifier: AGPL-3.0-only
// Reference renderer script of an Electron host (L92): the preload exposed
// the host contract on `window.ameeshHost`; connectHost binds it to the page.
import { connectHost } from "@ameesh/ui";

const host = window.ameeshHost;
const off = connectHost(host, { root: document, mesh: "acme" });

// The preload owns the `ameesh:navigate` channel and exposes a bounded
// subscription; the application's router decides what a target means.
const offNavigate = host.onNavigate((target) => {
  console.log("navigate", target);
});

window.addEventListener("beforeunload", () => {
  offNavigate();
  off();
});

// SPDX-License-Identifier: AGPL-3.0-only
// Reference renderer script of an Electron host (L92): the preload exposed
// the host contract on `window.ameeshHost`; connectHost binds it to the page.
import { connectHost } from "@ameesh/ui";

const host = window.ameeshHost;
const off = connectHost(host, { root: document, mesh: "acme" });

window.addEventListener("ameesh:navigate", (event) => {
  // the application's router decides; the components only raised the event
  console.log("navigate", event.detail);
});
window.addEventListener("beforeunload", off);

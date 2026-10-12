// SPDX-License-Identifier: AGPL-3.0-only
// @ameesh/ui (L88): embeddable components of an ameesh mesh. Importing this
// module defines the elements (once, even if loaded twice).
import { AmeeshApp } from "./components/ameesh-app";
import { AmeeshConfig } from "./components/ameesh-config";

export { ApiError, createClient, expand } from "./api/client";
export type { Body, Client, ClientOptions, ReadPath, Schemas, TokenProvider } from "./api/client";
export { EventStreamParser, readStream } from "./sse";
export type { StreamEvent, StreamOptions } from "./sse";
export { LANGUAGES, translate } from "./i18n/index";
export { AmeeshApp, AmeeshConfig };
export { configOf } from "./components/ameesh-config";

for (const [name, element] of [["ameesh-config", AmeeshConfig], ["ameesh-app", AmeeshApp]] as const) {
  if (!customElements.get(name)) customElements.define(name, element);
}

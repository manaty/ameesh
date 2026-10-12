// SPDX-License-Identifier: AGPL-3.0-only
// <ameesh-config api-base="…">: the mesh the components inside it talk to
// (study §2.2). The host sets `tokenProvider` once; the components never
// keep a token. With `token-form` and no provider (the standalone page),
// it asks for the token and keeps it in memory only, never in storage.
import { LitElement, css, html } from "lit";
import { createClient, type Client, type TokenProvider } from "../api/client";
import { languageOf, translate } from "../i18n/index";
import { base } from "../theme";

export const CONFIG_CHANGED = "ameesh:config-changed";

export class AmeeshConfig extends LitElement {
  static override properties = {
    apiBase: { attribute: "api-base" },
    tokenForm: { attribute: "token-form", type: Boolean },
    tokenFile: { attribute: "token-file" },
    theme: { reflect: true },
    tokenProvider: { attribute: false },
    connected: { state: true },
  };
  static override styles = [base, css`
    :host { display: contents; }
    form { display: grid; gap: 8px; max-width: 32rem; margin: 2rem auto; padding: 16px;
           background: var(--_surface); border: 1px solid var(--_grid);
           border-radius: var(--_radius); }
    h2 { margin: 0; font-size: 1.1rem; }
    p { margin: 0; }
  `];

  apiBase = "";
  tokenForm = false;
  tokenFile = "~/.config/ameesh/serve-token";
  theme = "auto";
  tokenProvider: TokenProvider | undefined;
  connected = false;
  #client: Client | null = null;
  #memoryToken: string | null = null;

  /** The client of this mesh, null until `api-base` is known. */
  get client(): Client | null {
    if (!this.#client) {
      const baseUrl = this.apiBase || (this.tokenForm ? location.origin : "");
      if (!baseUrl) return null;
      this.#client = createClient({ baseUrl, tokenProvider: () => this.#token() });
    }
    return this.#client;
  }

  async #token(): Promise<string | null> {
    if (this.tokenProvider) return this.tokenProvider();
    return this.#memoryToken;
  }

  /** Forget the token kept by the form (the provider is the host's). */
  disconnect(): void {
    this.#memoryToken = null;
    this.connected = false;
    this.#changed();
  }

  override willUpdate(changed: Map<string, unknown>): void {
    if (changed.has("apiBase") || changed.has("tokenForm")) this.#client = null;
    if (changed.has("tokenProvider")) this.connected = Boolean(this.tokenProvider);
  }

  override updated(changed: Map<string, unknown>): void {
    if (changed.has("apiBase") || changed.has("tokenProvider") || changed.has("tokenForm")) {
      this.#changed();
    }
    if (changed.has("theme")) {
      for (const el of this.querySelectorAll("*")) {
        if (el.tagName.startsWith("AMEESH-") && !el.hasAttribute("theme")) {
          el.setAttribute("theme", this.theme);
        }
      }
    }
  }

  #changed(): void {
    this.dispatchEvent(new CustomEvent(CONFIG_CHANGED, { bubbles: false }));
  }

  #submit(event: SubmitEvent): void {
    event.preventDefault();
    const input = this.renderRoot.querySelector("input");
    const token = input?.value.trim() ?? "";
    if (!token) return;
    this.#memoryToken = token;
    if (input) input.value = "";
    this.connected = true;
    this.#changed();
  }

  override render() {
    const lang = languageOf(this);
    const t = (key: string, params = {}) => translate(lang, key, params);
    const form = this.tokenForm && !this.tokenProvider && !this.connected;
    if (!this.client) {
      return html`<p role="alert" class="critical">${t("config.missing")}</p><slot></slot>`;
    }
    if (!form) return html`<slot></slot>`;
    return html`
      <form @submit=${this.#submit} aria-labelledby="title">
        <h2 id="title">${t("config.title")}</h2>
        <label for="token">${t("config.token")}</label>
        <input id="token" type="password" autocomplete="off" spellcheck="false"
               aria-describedby="help" required>
        <p id="help" class="muted">${t("config.token.help", { file: this.tokenFile })}</p>
        <button type="submit">${t("config.connect")}</button>
      </form>`;
  }
}

/** The nearest <ameesh-config> around `element`, across shadow roots, else
 * the first one of the document. */
export function configOf(element: Element): AmeeshConfig | null {
  let node: Node | null = element;
  while (node) {
    if (node instanceof Element && node.localName === "ameesh-config") {
      return node as AmeeshConfig;
    }
    node = node.parentNode ?? (node instanceof ShadowRoot ? node.host : null);
  }
  return document.querySelector("ameesh-config") as AmeeshConfig | null;
}

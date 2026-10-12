// SPDX-License-Identifier: AGPL-3.0-only
// <ameesh-app>: the default assembly of the components, full screen (the
// standalone page /ui/), with internal navigation (study §2.2). L88 gives
// the frame: who is connected to which mesh, in which mode; the screens
// arrive with L89. Outputs are DOM events (`ameesh:*`), never navigation.
import { LitElement, css, html } from "lit";
import { ApiError, type Client, type Schemas } from "../api/client";
import { languageOf, pick, translate, type Params } from "../i18n/index";
import { base } from "../theme";
import { CONFIG_CHANGED, UNAUTHORIZED, configOf, type AmeeshConfig } from "./ameesh-config";

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; discovery: Schemas["ameesh-discovery-1"]; me: Schemas["ameesh-me-1"] }
  | { kind: "error"; code: string };

export class AmeeshApp extends LitElement {
  static override properties = {
    client: { attribute: false },
    theme: { reflect: true },
    standalone: { type: Boolean },
    state: { state: true },
  };
  static override styles = [base, css`
    :host { display: block; min-height: 100%; background: var(--_page); }
    header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px;
             padding: 12px 16px; border-bottom: 1px solid var(--_grid);
             background: var(--_surface); }
    h1 { font-size: 1.25rem; margin: 0; }
    nav ul { display: flex; gap: 8px; list-style: none; margin: 0; padding: 8px 16px; }
    nav a { color: var(--_ink); padding: 2px 6px; border-radius: 4px; }
    nav a[aria-current="page"] { font-weight: 600; text-decoration: none;
                                 box-shadow: inset 0 -2px var(--_build); }
    main { max-width: 1200px; margin: 0 auto; padding: 16px; }
    .error { display: flex; gap: 12px; align-items: center; }
  `];

  /** Set by the host; otherwise that of the nearest <ameesh-config>. */
  client: Client | null = null;
  theme = "auto";
  /** The standalone page: the human's language (`GET /me`) comes first and
   * becomes the page's; embedded, the host page is never touched. */
  standalone = false;
  state: State = { kind: "idle" };
  #config: AmeeshConfig | null = null;
  #load: AbortController | null = null;
  #onConfig = () => void this.refresh();

  override connectedCallback(): void {
    super.connectedCallback();
    this.#config = configOf(this);
    this.#config?.addEventListener(CONFIG_CHANGED, this.#onConfig);
    void this.refresh();
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.#config?.removeEventListener(CONFIG_CHANGED, this.#onConfig);
    this.#load?.abort();
  }

  override updated(changed: Map<string, unknown>): void {
    if (changed.has("client") && changed.get("client") !== undefined) void this.refresh();
  }

  get #client(): Client | null {
    return this.client ?? this.#config?.client ?? null;
  }

  /** Reload who is connected to which mesh. */
  async refresh(): Promise<void> {
    this.#load?.abort();
    const client = this.#client;
    if (!client) {
      this.state = { kind: "idle" };
      return;
    }
    const load = this.#load = new AbortController();
    this.state = { kind: "loading" };
    try {
      const [discovery, me] = await Promise.all([
        client.get("/.well-known/ameesh", { signal: load.signal }),
        client.get("/api/v1/me", { signal: load.signal }),
      ]);
      if (load.signal.aborted) return;
      this.state = { kind: "ready", discovery, me };
      if (this.standalone) document.documentElement.lang = pick(me.language);
    } catch (error) {
      if (load.signal.aborted) return;
      const code = error instanceof ApiError ? error.code : "network";
      this.state = { kind: "error", code };
      if (code === "unauthorized") {
        this.dispatchEvent(new CustomEvent(UNAUTHORIZED, { bubbles: true, composed: true }));
      }
    }
  }

  #t(key: string, params: Params = {}): string {
    const me = this.state.kind === "ready" ? this.state.me.language : null;
    const lang = this.standalone && me ? pick(me) : languageOf(this, me);
    return translate(lang, key, params);
  }

  #error(code: string): string {
    if (code === "unauthorized") return this.#t("error.unauthorized");
    if (code === "unavailable") return this.#t("error.unavailable");
    if (code === "network") return this.#t("error.network");
    return this.#t("error.other", { code });
  }

  override render() {
    const state = this.state;
    const ready = state.kind === "ready" ? state : null;
    return html`
      <header part="header">
        <h1>${this.#t("app.title")}</h1>
        ${ready ? html`
          <span>${this.#t("app.connected", {
            organisation: ready.discovery.organisation ?? ready.me.human })}</span>
          <span class="muted">${this.#t("app.mesh", {
            version: ready.discovery.version, mode: ready.discovery.mode })}</span>` : ""}
      </header>
      <nav aria-label=${this.#t("app.nav")}>
        <ul><li><a href="#overview" aria-current="page">${this.#t("app.home")}</a></li></ul>
      </nav>
      <main id="overview" aria-live="polite" aria-busy=${state.kind === "loading"}>
        ${state.kind === "loading" ? html`<p class="muted">${this.#t("app.loading")}</p>` : ""}
        ${state.kind === "error" ? html`
          <div class="error" role="alert">
            <p class="critical">${this.#error(state.code)}</p>
            <button type="button" @click=${() => this.refresh()}>${this.#t("app.retry")}</button>
          </div>` : ""}
        ${ready ? html`<p>${this.#t("app.empty")}</p>` : ""}
      </main>`;
  }
}

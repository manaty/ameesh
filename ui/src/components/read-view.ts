// SPDX-License-Identifier: AGPL-3.0-only
import { LitElement, css, html, nothing } from "lit";
import { ApiError, type Client } from "../api/client";
import { formatDate, languageOf, translate, type Params } from "../i18n/index";
import { subscribe, type LiveState } from "../live";
import { base } from "../theme";
import { CONFIG_CHANGED, UNAUTHORIZED, configOf, type AmeeshConfig } from "./ameesh-config";

export type Row = Record<string, unknown>;
export const object = (value: unknown): Row => value && typeof value === "object" && !Array.isArray(value) ? value as Row : {};
export const rows = (value: unknown): Row[] => Array.isArray(value) ? value.map(object) : [];
export const label = (value: unknown): string => typeof value === "string" || typeof value === "number" ? String(value) : "—";
export const number = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;

export abstract class ReadView extends LitElement {
  static override properties = {
    client: { attribute: false }, project: {}, agent: {}, lot: {}, theme: { reflect: true },
    data: { state: true }, loading: { state: true }, error: { state: true }, live: { state: true },
  };
  static override styles = [base, css`
    :host { display: block; min-width: 0; }
    header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-bottom: 16px; }
    h2 { margin: 0; font-size: 1.2rem; flex: 1; }
    h3 { margin: 0 0 12px; font-size: 1.05rem; }
    article { padding: 16px; background: var(--_surface); border: 1px solid var(--_grid); border-radius: var(--_radius); min-width: 0; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 20rem), 1fr)); gap: 16px; }
    p, li, dd, td, th, button { overflow-wrap: anywhere; }
    p { margin: 8px 0; } ul { padding-inline-start: 20px; }
    .scroll { max-width: 100%; overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; }
    caption { text-align: start; padding-bottom: 8px; }
    th, td { text-align: start; vertical-align: top; padding: 8px; border-bottom: 1px solid var(--_grid); }
    dl { display: grid; grid-template-columns: minmax(7rem, 1fr) 2fr; gap: 8px; margin: 0; }
    dt { color: var(--_muted); } dd { margin: 0; }
    .text { white-space: pre-wrap; overflow-wrap: anywhere; }
    .link { border: 0; padding: 0; color: var(--_ink); background: none; text-decoration: underline; text-align: start; }
    .track { display: flex; width: 100%; height: 12px; border-radius: 3px; overflow: hidden; background: var(--_grid); }
    .build { fill: var(--_build); } .review { fill: var(--_review); } .merge { fill: var(--_merge); }
    meter { width: 100%; }
    @media (max-width: 480px) { dl { grid-template-columns: 1fr; gap: 4px; } dd { margin-bottom: 8px; } }
  `];
  client: Client | null = null;
  project = "";
  agent = "";
  lot = "";
  theme = "auto";
  data: Row | null = null;
  loading = false;
  error = "";
  live: LiveState = "waiting";
  abstract headingKey: string;
  protected required: "project" | "agent" | null = null;
  protected abstract load(client: Client, signal: AbortSignal): Promise<unknown>;
  protected abstract content(data: Row): unknown;
  #config: AmeeshConfig | null = null;
  #signature: unknown[] = [];
  #abort: AbortController | null = null;
  #unsubscribe: (() => void) | null = null;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #again = false;
  #configChanged = () => { this.#signature = []; this.#sync(); };

  override connectedCallback(): void {
    super.connectedCallback();
    this.#config = configOf(this);
    this.#config?.addEventListener(CONFIG_CHANGED, this.#configChanged);
    this.#sync();
  }
  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.#config?.removeEventListener(CONFIG_CHANGED, this.#configChanged);
    this.#unsubscribe?.(); this.#unsubscribe = null;
    this.#abort?.abort(); this.#abort = null;
    this.#signature = [];
    if (this.#timer !== null) clearTimeout(this.#timer); this.#timer = null;
  }
  override willUpdate(): void { this.#sync(); }
  #sync(): void {
    if (!this.isConnected) return;
    const client = this.client ?? this.#config?.client ?? null;
    const signature = [client, this.project, this.agent, this.lot];
    if (signature.every((value, i) => value === this.#signature[i])) return;
    this.#signature = signature;
    if (this.#timer !== null) clearTimeout(this.#timer); this.#timer = null;
    this.#abort?.abort(); this.#abort = null;
    this.#unsubscribe?.(); this.#unsubscribe = null;
    this.data = null; this.error = ""; this.loading = false; this.#again = false;
    this.live = "waiting";
    if (!client || (this.required && !this[this.required])) return;
    this.#unsubscribe = subscribe(client, {
      event: () => this.#schedule(), state: (state) => { this.live = state; },
      error: (status) => {
        if (status !== 401 && status !== 403) return;
        this.#abort?.abort(); this.#abort = null; this.loading = false;
        if (this.#timer !== null) clearTimeout(this.#timer); this.#timer = null;
        this.#again = false; this.data = null; this.error = status === 401 ? "unauthorized" : "forbidden";
        if (status === 401) this.dispatchEvent(new CustomEvent(UNAUTHORIZED, { bubbles: true, composed: true }));
      },
    });
    void this.refresh();
  }
  #schedule(): void {
    if (this.#timer !== null) return;
    // Coalesce bursts: one bounded refresh instead of one request per row/event.
    this.#timer = setTimeout(() => {
      this.#timer = null;
      if (this.isConnected) void this.refresh();
    }, 1000);
  }
  async refresh(): Promise<void> {
    const client = this.client ?? this.#config?.client ?? null;
    if (!client || !this.isConnected || (this.required && !this[this.required])) return;
    if (this.#abort) { this.#again = true; return; }
    const abort = this.#abort = new AbortController();
    this.loading = true; this.error = "";
    try {
      const data = await this.load(client, abort.signal);
      if (!abort.signal.aborted) this.data = object(data);
    } catch (error) {
      if (abort.signal.aborted) return;
      this.error = error instanceof ApiError ? error.code : "network";
      // A refused view must not retain data from a previous login.
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) this.data = null;
      if (this.error === "unauthorized") this.dispatchEvent(new CustomEvent(UNAUTHORIZED, { bubbles: true, composed: true }));
    } finally {
      if (this.#abort === abort) {
        this.#abort = null; this.loading = false;
        if (this.#again) { this.#again = false; this.#schedule(); }
      }
    }
  }
  protected t(key: string, params: Params = {}): string { return translate(languageOf(this), key, params); }
  protected date(value: unknown): string {
    const ts = number(value);
    return ts === null || !Number.isFinite(new Date(ts * 1000).getTime()) ? "—" : formatDate(languageOf(this), ts);
  }
  protected money(value: unknown): string {
    const n = number(value);
    return n === null ? "—" : new Intl.NumberFormat(languageOf(this), { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(n);
  }
  protected state(value: unknown): string {
    const code = label(value); const text = this.t("state." + code);
    return text === "state." + code ? code : text;
  }
  protected open(kind: string, detail: Row): void {
    this.dispatchEvent(new CustomEvent("ameesh:open-" + kind, { detail, bubbles: true, composed: true }));
  }
  protected empty(): unknown { return html`<p class="muted">${this.t("view.empty")}</p>`; }
  override render() {
    const code = ["unauthorized", "unavailable", "network", "not_found", "forbidden"].includes(this.error) ? this.error : "other";
    return html`<section aria-label=${this.t(this.headingKey)} aria-busy=${this.loading}>
      <header><h2>${this.t(this.headingKey)}</h2>
        ${this.data ? html`<span class="muted" role="status">${this.t("stream." + this.live)}</span>` : nothing}
        <button type="button" @click=${() => this.refresh()} ?disabled=${this.loading}>${this.t("view.refresh")}</button></header>
      ${this.loading && !this.data ? html`<p role="status">${this.t("app.loading")}</p>` : nothing}
      ${this.error ? html`<p role="alert" class="critical">${this.t("error." + code, { code: this.error })}</p>` : nothing}
      ${this.required && !this[this.required] ? html`<p>${this.t("view.choose." + this.required)}</p>` : nothing}
      ${this.data ? this.content(this.data) : nothing}
    </section>`;
  }
}

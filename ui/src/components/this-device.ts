// SPDX-License-Identifier: AGPL-3.0-only
// <ameesh-this-device> (L93, study §4): "my workstation". The screen reads the
// LOCAL state of this machine's runners — never a mesh API, never a token.
// The state arrives from the host (the desktop's main process reads the
// runner's Unix socket): the component only renders it and raises the stop
// event. A browser page cannot open a machine socket; without a provider it
// says so instead of pretending.
import { LitElement, css, html, nothing } from "lit";
import { formatDate, languageOf, translate, type Params } from "../i18n/index";
import { base } from "../theme";

export interface DeviceLot {
  id?: number | null;
  title?: string;
  state?: string;
  app?: string;
}

export interface DeviceAgent {
  name?: string;
  persona?: string;
  harness?: string;
  status?: string;
  status_text?: string;
  project?: string;
  lot?: DeviceLot | null;
  turn?: { active?: boolean; started_ts?: number | null; label?: string };
  lease?: { live?: boolean; owner?: string; expires_ts?: number | null };
  sandbox?: {
    measured?: boolean; cpu?: number | null; cpu_some?: number | null;
    memory_bytes?: number | null; memory_max_bytes?: number | null;
    memory_used?: number | null; throttled?: number | null; network?: number | null;
  };
  runner?: string;
  since_ts?: number | null;
  last_turn_ts?: number | null;
}

export interface DeviceMesh {
  mesh?: string;
  organisation?: string;
  agents?: DeviceAgent[];
}

export interface DeviceRunner {
  id?: string;
  pid?: number;
  since_ts?: number | null;
  socket?: string;
  control_socket?: string;
}

export interface DeviceState {
  schema?: string;
  error?: string;
  message?: string;
  generated_ts?: number;
  host?: string;
  runners?: DeviceRunner[];
  meshes?: DeviceMesh[];
  resources?: Record<string, number | null>;
  compute?: { gate?: { state?: string; seq?: number; reason?: string } | null;
    lease?: { id?: string; expires_ts?: number } | null };
  unmeasured?: string[];
}

/** The host supplies the state; the component never opens the socket itself. */
export type DeviceProvider = () => DeviceState | null | Promise<DeviceState | null>;

export class AmeeshThisDevice extends LitElement {
  static override properties = {
    state: { attribute: false },
    provider: { attribute: false },
    poll: { type: Number },
    theme: { reflect: true },
    error: { state: true },
    loading: { state: true },
  };
  static override styles = [base, css`
    :host { display: block; min-width: 0; }
    header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-bottom: 16px; }
    h2 { margin: 0; font-size: 1.2rem; flex: 1; }
    h3 { margin: 0 0 12px; font-size: 1.05rem; }
    section { margin-bottom: 20px; }
    article { padding: 16px; background: var(--_surface); border: 1px solid var(--_grid);
              border-radius: var(--_radius); min-width: 0; }
    .scroll { max-width: 100%; overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; }
    caption { text-align: start; padding-bottom: 8px; }
    th, td { text-align: start; vertical-align: top; padding: 8px; border-bottom: 1px solid var(--_grid); }
    th { white-space: nowrap; }
    p, dd, td, th, button { overflow-wrap: anywhere; }
    p { margin: 8px 0; }
    dl { display: grid; grid-template-columns: minmax(7rem, 1fr) 2fr; gap: 8px; margin: 0; }
    dt { color: var(--_muted); } dd { margin: 0; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .link { border: 0; padding: 0; color: var(--_ink); background: none; text-decoration: underline;
            text-align: start; }
    .muted { color: var(--_muted); }
    .critical { color: var(--_critical); }
    @media (max-width: 520px) { dl { grid-template-columns: 1fr; gap: 4px; } dd { margin-bottom: 8px; } }
  `];

  state: DeviceState | null = null;
  provider: DeviceProvider | null = null;
  /** Polling period when `provider` is set, in seconds (0: no polling). */
  poll = 5;
  theme = "auto";
  error = "";
  loading = false;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #disposed = false;

  override connectedCallback(): void {
    super.connectedCallback();
    this.#disposed = false;
    this.#schedule(0);
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.#disposed = true;
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
  }

  override willUpdate(changed: Map<string, unknown>): void {
    if (changed.has("provider") || changed.has("poll")) this.#schedule(0);
  }

  #schedule(delay: number): void {
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
    if (this.#disposed || !this.provider) return;
    this.#timer = setTimeout(() => void this.refresh(), Math.max(0, delay) * 1000);
  }

  /** Read the host's provider once; public so a host can force a refresh. */
  async refresh(): Promise<void> {
    if (!this.provider || this.#disposed) return;
    this.loading = true;
    try {
      const state = await this.provider();
      if (!this.#disposed) { this.state = state ?? null; this.error = ""; }
    } catch (err) {
      if (!this.#disposed) this.error = err instanceof Error ? err.message : String(err);
    } finally {
      if (!this.#disposed) {
        this.loading = false;
        const period = Number(this.poll);
        if (Number.isFinite(period) && period > 0) this.#schedule(period);
      }
    }
  }

  private t(key: string, params: Params = {}): string {
    return translate(languageOf(this), key, params);
  }

  private date(value: unknown): string {
    return typeof value === "number" && Number.isFinite(value)
      ? formatDate(languageOf(this), value) : "—";
  }

  private bytes(value: unknown): string {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    return new Intl.NumberFormat(languageOf(this), { maximumFractionDigits: 1 })
      .format(value / 2 ** 30) + " GiB";
  }

  private percent(value: unknown): string {
    if (typeof value !== "number" || !Number.isFinite(value)) return "—";
    return new Intl.NumberFormat(languageOf(this), { style: "percent", maximumFractionDigits: 1 })
      .format(value);
  }

  private number(value: unknown): string {
    return typeof value === "number" && Number.isFinite(value)
      ? new Intl.NumberFormat(languageOf(this), { maximumFractionDigits: 2 }).format(value) : "—";
  }

  private stop(agent: DeviceAgent, runner: DeviceRunner | undefined, mesh: string,
              mode: "clean" | "immediate"): void {
    this.dispatchEvent(new CustomEvent("ameesh:stop", {
      detail: { agent: agent.name, runner: runner?.id, mesh,
        control_socket: runner?.control_socket, mode },
      bubbles: true, composed: true,
    }));
  }

  private open(kind: "agent" | "lot", detail: Record<string, unknown>): void {
    this.dispatchEvent(new CustomEvent("ameesh:open-" + kind, {
      detail, bubbles: true, composed: true,
    }));
  }

  private agentRow(agent: DeviceAgent, runners: DeviceRunner[], mesh: string): unknown {
    const runner = runners.find((item) => item.id === agent.runner);
    const sandbox = agent.sandbox ?? {};
    const lot = agent.lot ?? {};
    const turn = agent.turn ?? {};
    const lease = agent.lease ?? {};
    return html`<tr>
      <th scope="row"><button class="link" @click=${() => this.open("agent", { kind: "agent",
          id: agent.name, row: agent })}>${agent.name ?? "—"}</button>
        <p class="muted">${agent.harness ?? "—"}${agent.status_text ? html` · ${agent.status_text}` : nothing}</p></th>
      <td>${agent.project ?? "—"}</td>
      <td>${lot.id ? html`<button class="link" @click=${() => this.open("lot", { kind: "lot",
          id: String(lot.id), project: lot.app ?? agent.project, row: lot })}>#${lot.id} ${lot.title ?? ""}</button>`
        : "—"}</td>
      <td>${agent.status ?? "—"}</td>
      <td>${turn.active ? html`${this.t("device.turn.active")}<p class="muted">${this.date(turn.started_ts)}</p>`
        : this.t("device.turn.none")}</td>
      <td>${sandbox.measured ? html`${this.number(sandbox.cpu)} CPU
        <p class="muted">${this.bytes(sandbox.memory_bytes)} / ${this.bytes(sandbox.memory_max_bytes)}
          · ${this.percent(sandbox.memory_used)}</p>`
        : this.t("device.sandbox.unmeasured")}</td>
      <td>${lease.live ? this.date(lease.expires_ts) : this.t("device.lease.none")}</td>
      <td><div class="actions">
        <button type="button" @click=${() => this.stop(agent, runner, mesh, "clean")}
          title=${this.t("device.stop.clean.help")}>${this.t("device.stop.clean")}</button>
        <button type="button" class="critical" @click=${() => this.stop(agent, runner, mesh, "immediate")}
          title=${this.t("device.stop.immediate.help")}>${this.t("device.stop.immediate")}</button>
      </div></td></tr>`;
  }

  protected content(state: DeviceState): unknown {
    const runners = state.runners ?? [];
    const resources = state.resources ?? {};
    const compute = state.compute ?? {};
    const gate = compute.gate ?? null;
    const meshes = state.meshes ?? [];
    return html`
      <p>${this.t("device.host", { host: state.host ?? "?" })}
        · ${this.t("device.generated", { at: this.date(state.generated_ts) })}
        ${this.loading ? html` · ${this.t("app.loading")}` : nothing}</p>
      <section aria-label=${this.t("device.resources")}><article><h3>${this.t("device.resources")}</h3>
        <dl>
          <dt>${this.t("device.resources.cpu")}</dt><dd>${this.number(resources.cpu_count)}</dd>
          <dt>${this.t("device.resources.load")}</dt><dd>${this.number(resources.load1)}</dd>
          <dt>${this.t("device.resources.memory")}</dt><dd>${this.bytes(resources.mem_available_bytes)}
            / ${this.bytes(resources.mem_total_bytes)}</dd>
          <dt>${this.t("device.resources.turns")}</dt><dd>${this.number(resources.turns_in_progress)}</dd>
        </dl>
        ${gate ? html`<p>${this.t("device.compute.gate", { state: gate.state ?? "?",
            reason: gate.reason ?? "?" })}</p>` : html`<p class="muted">${this.t("device.compute.none")}</p>`}
        ${compute.lease ? html`<p>${this.t("device.compute.lease", {
            id: compute.lease.id ?? "—", at: this.date(compute.lease.expires_ts) })}</p>` : nothing}
        ${state.unmeasured?.length ? html`<p class="muted">${this.t("device.unmeasured",
            { fields: state.unmeasured.join(", ") })}</p>` : nothing}
      </article></section>
      ${meshes.length ? meshes.map((mesh) => html`<section aria-label=${mesh.organisation ?? mesh.mesh ?? "?"}>
        <article><h3>${mesh.organisation ?? mesh.mesh ?? "?"} <span class="muted">${mesh.mesh ?? ""}</span></h3>
        ${(mesh.agents ?? []).length ? html`<div class="scroll" tabindex="0" role="region"
            aria-label=${this.t("device.agents")}><table>
          <caption>${this.t("device.agents")}</caption>
          <thead><tr>${["persona", "project", "lot", "state", "turn", "sandbox", "lease", "actions"]
            .map((key) => html`<th scope="col">${this.t("device.field." + key)}</th>`)}</tr></thead>
          <tbody>${(mesh.agents ?? []).map((agent) => this.agentRow(agent, runners, mesh.mesh ?? ""))}</tbody>
        </table></div>` : html`<p class="muted">${this.t("view.empty")}</p>`}
        </article></section>`) : html`<p class="muted">${this.t("view.empty")}</p>`}`;
  }

  override render() {
    return html`<section aria-label=${this.t("view.this-device")} aria-busy=${this.loading}>
      <header><h2>${this.t("view.this-device")}</h2>
        ${this.provider ? html`<button type="button" @click=${() => this.refresh()}
          ?disabled=${this.loading}>${this.t("view.refresh")}</button>` : nothing}</header>
      ${this.error ? html`<p role="alert" class="critical">${this.t("device.error", { message: this.error })}</p>` : nothing}
      ${this.state?.error ? html`<p role="alert" class="critical">${this.t("device.error",
          { message: this.state.message ?? this.state.error })}</p>` : nothing}
      ${this.state && !this.state.error ? this.content(this.state)
        : this.state ? nothing
        : html`<p class="muted">${this.provider ? this.t("app.loading") : this.t("device.provider.missing")}</p>`}
    </section>`;
  }
}

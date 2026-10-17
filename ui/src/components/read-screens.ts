// SPDX-License-Identifier: AGPL-3.0-only
import { html, svg, nothing } from "lit";
import { languageOf } from "../i18n/index";
import type { Client } from "../api/client";
import { ReadView, label, number, object, rows, type Row } from "./read-view";

export class AmeeshProjects extends ReadView {
  headingKey = "view.projects";
  protected load(client: Client, signal: AbortSignal) {
    return client.get("/api/v1/projects", { query: this.project ? { project: this.project } : {}, signal });
  }
  protected content(data: Row) {
    const projects = rows(data.projects);
    return projects.length ? html`<div class="cards">${projects.map((p) => html`<article>
      <h3><button class="link" @click=${() => this.open("project", { project: p.name })}>${label(p.name)}</button></h3>
      <dl><dt>${this.t("field.lots")}</dt><dd>${label(p.lots_open)}</dd>
        <dt>${this.t("field.spend24")}</dt><dd>${this.money(p.usd_24h_token)}</dd>
        <dt>${this.t("field.estimated24")}</dt><dd>${this.money(p.usd_24h)}</dd>
        <dt>${this.t("field.unread")}</dt><dd>${label(p.unread)}</dd></dl>
      <ul>${rows(p.agents).map((a) => html`<li>
        <button class="link" @click=${() => this.open("agent", { agent: a.name, project: p.name })}>${label(a.name)}</button>
        — ${this.state(a.state)}${a.reason ? html` · ${label(a.reason)}` : nothing}
        ${a.lot ? html` · <button class="link" @click=${() => this.open("lot", { lot: object(a.lot).id, project: p.name })}>#${label(object(a.lot).id)} ${label(object(a.lot).title)}</button>` : nothing}
      </li>`)}</ul>
      ${rows(p.lots_without_agent).length ? html`<h3>${this.t("field.unassigned")}</h3><ul>${rows(p.lots_without_agent).map((lot) => html`<li><button class="link" @click=${() => this.open("lot", { project: p.name, lot: lot.id })}>#${label(lot.id)} ${label(lot.title)}</button></li>`)}</ul>` : nothing}
      ${Array.isArray(p.warnings) ? p.warnings.map((warning) => html`<p class="critical">${label(warning)}</p>`) : nothing}
    </article>`)}</div>` : this.empty();
  }
}

export class AmeeshTimeline extends ReadView {
  headingKey = "view.timeline";
  protected override required = "project" as const;
  protected load(client: Client, signal: AbortSignal) {
    return client.get("/api/v1/projects/{project}/progress", { path: { project: this.project }, signal });
  }
  #track(lot: Row, data: Row) {
    const m = object(lot.milestones), window = object(data.window);
    const from = number(window.from_ts) ?? number(m.requested);
    const to = number(window.to_ts) ?? number(data.generated_ts);
    if (from === null || to === null || to <= from) return nothing;
    const end = number(m.merged) ?? number(object(lot.closed).at_ts) ?? number(object(lot.stale).since_ts) ?? to;
    const phases = [
      ["build", number(m.requested), number(m.frozen) ?? number(m.verdict) ?? end],
      ["review", number(m.frozen), number(m.verdict) ?? end],
      [lot.verdict === "blocked" ? "build" : "merge", number(m.verdict), end],
    ] as const;
    // SVG presentation attributes work under the standalone page's CSP;
    // initial inline style attributes are deliberately forbidden there.
    return html`<svg class="track" viewBox="0 0 100 12" preserveAspectRatio="none" aria-hidden="true">
      ${phases.map(([phase, start, finish]) => {
        const x = Math.max(0, Math.min(100, ((start ?? from) - from) / (to - from) * 100));
        const width = start === null ? 0 : Math.max(0, Math.min(finish, to) - Math.max(start, from)) / (to - from) * 100;
        return svg`<rect class=${phase} x=${x} y="0" width=${width} height="12"></rect>`;
      })}</svg>`;
  }
  protected content(data: Row) {
    const lots = rows(data.lots);
    if (!lots.length) return this.empty();
    return html`<p>${this.t("timeline.legend")}</p><div class="scroll" tabindex="0" role="region" aria-label=${this.t("timeline.table")}>
      <table><caption>${this.project} · ${this.date(object(data.window).from_ts)} – ${this.date(object(data.window).to_ts ?? data.generated_ts)}</caption>
      <thead><tr>${["lot", "state", "assignee", "requested", "frozen", "verdict", "merged"].map((key) => html`<th scope="col">${this.t("field." + key)}</th>`)}</tr></thead>
      <tbody>${lots.map((lot) => html`<tr><th scope="row"><button class="link" @click=${() => this.open("lot", { project: this.project, lot: lot.id })}>#${label(lot.id)} ${label(lot.title)}</button>${this.#track(lot, data)}</th>
        <td>${this.state(lot.state)}${lot.waiting_for ? html`<p>${label(object(lot.waiting_for).label)}</p>` : nothing}</td>
        <td>${lot.assignee ? html`<button class="link" @click=${() => this.open("agent", { agent: lot.assignee, project: this.project })}>${label(lot.assignee)}</button>` : "—"}</td>
        ${["requested", "frozen", "verdict", "merged"].map((key) => html`<td>${this.date(object(lot.milestones)[key])}</td>`)}</tr>`)}</tbody></table></div>`;
  }
}

export class AmeeshAgent extends ReadView {
  headingKey = "view.agent";
  protected override required = "agent" as const;
  protected async load(client: Client, signal: AbortSignal) {
    const [detail, overview] = await Promise.all([
      client.get("/api/v1/agents/{agent}", { path: { agent: this.agent }, signal }),
      client.get("/api/v1/projects", { signal }),
    ]);
    const current = rows(object(overview).projects).flatMap((p) => rows(p.agents)).find((a) => a.name === this.agent);
    return { agent: { ...object(detail.agent), ...current } };
  }
  protected content(data: Row) {
    const a = object(data.agent), lot = object(a.lot);
    return html`<article><h3>${label(a.name)}</h3><dl>
      <dt>${this.t("field.state")}</dt><dd>${this.state(a.state ?? a.status)}</dd>
      <dt>${this.t("field.reason")}</dt><dd>${label(a.reason ?? a.status_text)}</dd>
      <dt>${this.t("field.host")}</dt><dd>${label(a.host)}</dd>
      <dt>${this.t("field.harness")}</dt><dd>${label(a.harness)}</dd>
      <dt>${this.t("field.since")}</dt><dd>${this.date(a.since_ts ?? a.updated_ts)}</dd>
      <dt>${this.t("field.unread")}</dt><dd>${label(a.unread)}</dd>
      <dt>${this.t("field.spend24")}</dt><dd>${a.payment === "token" ? this.money(a.usd_24h) : "—"}</dd>
      <dt>${this.t("field.estimated24")}</dt><dd>${this.money(a.usd_24h)}</dd>
      <dt>${this.t("field.lot")}</dt><dd>${lot.id ? html`<button class="link" @click=${() => this.open("lot", { project: a.project ?? this.project, lot: lot.id })}>#${label(lot.id)} ${label(lot.title)}</button>` : "—"}</dd>
      <dt>${this.t("field.activity")}</dt><dd>${label(object(a.last_update).text ?? a.task)}</dd>
    </dl></article>`;
  }
}

export class AmeeshAlerts extends ReadView {
  headingKey = "view.alerts";
  protected load(client: Client, signal: AbortSignal) { return client.get("/api/v1/alerts", { signal }); }
  protected content(data: Row) {
    const alerts = rows(data.alerts);
    return alerts.length ? html`<div class="cards">${alerts.map((a) => html`<article>
      <h3>${label(a.title ?? a.type)}</h3><p>${label(a.message ?? a.reason ?? a.detail)}</p>
      <dl><dt>${this.t("field.severity")}</dt><dd>${label(a.severity ?? a.level)}</dd>
        <dt>${this.t("field.responsible")}</dt><dd>${label(a.responsible)}</dd></dl>
      ${a.agent ? html`<p><button class="link" @click=${() => this.open("agent", { agent: a.agent, project: a.project })}>${label(a.agent)}</button></p>` : nothing}
      ${a.lot || a.work_item_id ? html`<p><button class="link" @click=${() => this.open("lot", { lot: a.lot ?? a.work_item_id, project: a.project })}>#${label(a.lot ?? a.work_item_id)}</button></p>` : nothing}
    </article>`)}</div>` : this.empty();
  }
}

export class AmeeshHosts extends ReadView {
  headingKey = "view.hosts";
  protected load(client: Client, signal: AbortSignal) { return client.get("/api/v1/hosts", { signal }); }
  #bytes(value: unknown): string {
    const n = number(value);
    return n === null ? "—" : new Intl.NumberFormat(languageOf(this), { maximumFractionDigits: 1 }).format(n / 2 ** 30) + " GiB";
  }
  protected content(data: Row) {
    const hosts = rows(data.hosts);
    return hosts.length ? html`<div class="cards">${hosts.map((h) => {
      const latest = object(h.latest), pressure = object(h.pressure);
      return html`<article><h3>${label(h.host)}</h3><dl>
        <dt>${this.t("field.responsible")}</dt><dd>${label(h.responsible)}</dd>
        <dt>${this.t("field.pressure")}</dt><dd>${h.latest ? this.state(pressure.critical ? "critical" : pressure.blocked ? "warning" : "ok") : this.t("host.noReading")}</dd>
        <dt>${this.t("field.measured")}</dt><dd>${this.date(latest.sampled_ts)}</dd>
        <dt>${this.t("field.memory")}</dt><dd>${this.#bytes(latest.mem_available_bytes)}</dd>
        <dt>${this.t("field.swap")}</dt><dd>${this.#bytes(latest.swap_used_bytes)}</dd>
        <dt>${this.t("field.load")}</dt><dd>${label(latest.load1)}</dd>
        <dt>${this.t("field.turns")}</dt><dd>${label(latest.turns_in_progress)}</dd>
        <dt>${this.t("field.capacity")}</dt><dd>${label(h.max_agents)}</dd>
      </dl>${Array.isArray(pressure.breaches) ? rows(pressure.breaches).map((r) => html`<p class="critical">${label(r.key)} · ${label(r.value)} / ${label(r.limit)}</p>`) : nothing}
      <ul>${rows(h.fiches).map((f) => html`<li>${label(f.canon)} · ${label(f.admissions)} / ${label(f.max_agents)}</li>`)}</ul></article>`;
    })}</div>` : this.empty();
  }
}

export class AmeeshBudget extends ReadView {
  headingKey = "view.budget";
  protected async load(client: Client, signal: AbortSignal) {
    const [budget, progress] = await Promise.all([
      client.get("/api/v1/budget", { signal }),
      this.project ? client.get("/api/v1/projects/{project}/progress", { path: { project: this.project }, signal }) : null,
    ]);
    return { ...budget, detail: object(object(progress).budget) };
  }
  #gauge(gauge: Row) {
    const used = number(gauge.used), cap = number(gauge.pace_cap);
    return html`<p>${label(gauge.key)} · ${used === null ? "—" : Math.round(used * 100) + "%"}
      · ${this.t("field.pace")}: ${cap === null ? "—" : Math.round(cap * 100) + "%"}
      · ${this.t("field.reset")}: ${this.date(gauge.resets_ts)}</p>
      ${used === null ? nothing : html`<meter min="0" max="1" value=${Math.max(0, Math.min(1, used))} aria-label=${label(gauge.key)}>${Math.round(used * 100)}%</meter>`}`;
  }
  protected content(data: Row) {
    const limits = object(data.limits), spend = object(data.spend), detail = object(data.detail);
    return html`<div class="cards"><article><h3>${this.t("budget.mesh")}</h3><dl>
      <dt>${this.t("field.capHour")}</dt><dd>${limits.per_hour_usd === null ? this.t("budget.noCap") : this.money(limits.per_hour_usd)}</dd>
      <dt>${this.t("field.capDay")}</dt><dd>${limits.per_day_usd === null ? this.t("budget.noCap") : this.money(limits.per_day_usd)}</dd>
      <dt>${this.t("field.spendHour")}</dt><dd>${this.money(spend["1h_counted_usd"] ?? spend["1h_usd"])}</dd>
      <dt>${this.t("field.spend24")}</dt><dd>${this.money(spend["24h_counted_usd"] ?? spend["24h_usd"])}</dd>
      <dt>${this.t("budget.planValue")}</dt><dd>${this.money(object(object(detail.spend)["24h"]).plan_value_usd)}</dd>
    </dl></article><article><h3>${this.t("budget.paused")}</h3>
      ${rows(data.paused).length ? html`<ul>${rows(data.paused).map((a) => html`<li>${label(a.agent)} · ${label(a.reason)}</li>`)}</ul>` : this.empty()}</article></div>
      ${spend.error ? html`<p role="alert">${label(spend.error)}</p>` : nothing}
      ${Object.keys(object(limits.agents)).length ? html`<h3>${this.t("budget.agentCaps")}</h3><ul>${Object.entries(object(limits.agents)).map(([agent, caps]) => html`<li>${agent} · ${this.t("field.capHour")}: ${this.money(object(caps).per_hour_usd)} · ${this.t("field.capDay")}: ${this.money(object(caps).per_day_usd)}</li>`)}</ul>` : nothing}
      <h3>${this.t("budget.accounts")}</h3>
      ${!this.project ? html`<p>${this.t("budget.chooseProject")}</p>` : nothing}
      ${this.project && !rows(detail.accounts).length && !rows(detail.plans).length ? this.empty() : nothing}
      <div class="cards">${rows(detail.accounts).map((a) => html`<article><h3>${label(a.harness)} / ${label(a.account)} ${a.active ? this.t("budget.active") : ""}</h3>
        <p>${label(a.reason || a.why)}</p>${rows(a.gauges).map((g) => this.#gauge(g))}</article>`)}</div>
      ${rows(detail.plans).map((g) => html`<article><h3>${label(g.harness)}</h3>${this.#gauge(g)}</article>`)}
      ${rows(detail.real).length ? html`<h3>${this.t("budget.actualSpend")}</h3><ul>${rows(detail.real).map((r) => html`<li>${label(r.provider ?? r.harness)} · ${label(r.period)} · ${label(r.real_spent)} ${label(r.currency)}</li>`)}</ul>` : nothing}`;
  }
}

export class AmeeshThread extends ReadView {
  headingKey = "view.thread";
  protected override required = "project" as const;
  protected load(client: Client, signal: AbortSignal) {
    return this.lot ? client.get("/api/v1/threads/{project}/{lot}", { path: { project: this.project, lot: this.lot }, signal })
      : client.get("/api/v1/threads/{project}", { path: { project: this.project }, signal });
  }
  protected content(data: Row) {
    const entries = rows(data.entries);
    return entries.length ? html`<ol>${entries.map((entry) => html`<li><article>
      <h3>${label(entry.author)} → ${label(entry.recipient)}</h3>
      <p class="muted">${this.date(entry.ts)}</p><div class="text">${label(entry.text)}</div>
    </article></li>`)}</ol>` : this.empty();
  }
}

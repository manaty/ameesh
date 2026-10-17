// SPDX-License-Identifier: AGPL-3.0-only
// <ameesh-decisions> (L90, study §1 "Decisions to take", decision queue R17):
// the single queue of the human — gated actions waiting for an approval,
// requests filed by agents, lots waiting for a human, prose decisions. The
// "Approve" button does not approve: it files the request (L87's
// `POST /actions/{id}/approval-request`), then emits
// `ameesh:approval-requested` with the ameesh-approve link for the host to
// open in the foreground (contract §2.2: no component opens or navigates);
// standalone, the link stays a real anchor for the human to click. The
// passkey is signed on ameesh-approve's origin and the receipt comes back
// through the stream (`receipt.attached`).
import { html, nothing } from "lit";
import { ApiError, type Client } from "../api/client";
import { languageOf } from "../i18n/index";
import { ReadView, label, number, object, rows, type Row } from "./read-view";

/** A fresh `Idempotency-Key` (the API's pattern: 8 to 128 of [A-Za-z0-9_-]). */
export function idempotencyKey(): string {
  const bytes = new Uint8Array(16);
  const crypto = globalThis.crypto;
  if (crypto?.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The request filed for one action: the key that filed it, and the instant
 *  (epoch seconds) its link stops opening. */
type Filing = { expires: number; key: string };

/** When the filed request stops opening: the earlier of the link's own TTL
 *  (`link_exp`) and the request's `exp`, as ameesh-approve checks them; a
 *  reply without either never expires here. */
function expiryOf(result: Row): number {
  const stamps = [number(result.link_exp), number(result.exp)]
    .filter((stamp): stamp is number => stamp !== null);
  return stamps.length ? Math.min(...stamps) : Infinity;
}

export class AmeeshDecisions extends ReadView {
  static override properties = {
    ...ReadView.properties,
    filing: { state: true }, filed: { state: true }, failed: { state: true },
  };
  headingKey = "view.decisions";
  /** The action being filed right now (its button waits). */
  filing = "";
  /** action id → ameesh-approve link of the request already filed. */
  filed: Record<string, string> = {};
  /** action id → the refusal to show under its card. */
  failed: Record<string, string> = {};
  //: one key per action while its request is open: a retry after an uncertain
  //: network failure replays, never files twice. A renewal, once the expiry is
  //: known, replaces the key (`approve`), the old one only replaying the dead link.
  #keys = new Map<string, string>();
  //: what was filed per action: the key in flight and the link's expiry
  #filings = new Map<string, Filing>();

  protected load(client: Client, signal: AbortSignal) {
    return client.get("/api/v1/decisions", { signal });
  }

  #key(action: string): string {
    let key = this.#keys.get(action);
    if (!key) {
      key = idempotencyKey();
      this.#keys.set(action, key);
    }
    return key;
  }

  /** The API's seconds: the same unit as `link_exp`/`exp`. */
  #now(): number { return Math.floor(Date.now() / 1000); }

  /** The filed link of `action` is past its known expiry (410 at ameesh-approve). */
  #expired(action: string): boolean {
    const filed = this.#filings.get(action);
    return filed !== undefined && filed.expires <= this.#now();
  }

  /** File the approval request of `action`, keep its link, tell the host. */
  async approve(action: string): Promise<void> {
    const client = this.api;
    if (!client || !action || this.filing) return;
    this.filing = action;
    this.failed = { ...this.failed };
    delete this.failed[action];
    try {
      const filed = this.#filings.get(action);
      if (filed && filed.expires <= this.#now() && this.#keys.get(action) === filed.key) {
        // The filed link expired: this call is a renewal, and the old key would
        // only replay the dead link. A fresh key files a new request; an
        // uncertain retry of the renewal (same key as the attempt in flight) keeps it.
        this.#keys.delete(action);
      }
      const key = this.#key(action);
      const reply = await client.post("/api/v1/actions/{action}/approval-request", {
        path: { action },
        body: {},
        headers: { "Idempotency-Key": key },
      });
      const result = object(object(reply).result);
      const link = result.link;
      const url = typeof link === "string" ? link : "";
      if (url) {
        this.filed = { ...this.filed, [action]: url };
        this.#filings.set(action, { expires: expiryOf(result), key });
        // the host opens it in the foreground; the button here never approves
        this.dispatchEvent(new CustomEvent("ameesh:approval-requested", {
          detail: { action, url }, bubbles: true, composed: true }));
      }
      await this.refresh();
    } catch (error) {
      this.failed = { ...this.failed,
        [action]: error instanceof ApiError ? error.code : "network" };
    } finally {
      this.filing = "";
    }
  }

  #action(row: Row) {
    const action = label(row.action_id);
    const amount = number(row.amount);
    const currency = label(row.currency);
    const format = /^[A-Z]{3}$/.test(currency) ? new Intl.NumberFormat(languageOf(this), {
      style: "currency", currency, currencyDisplay: "code",
    }) : null;
    // Actions carry integer minor units; EUR 4200 means EUR 42.00, JPY 4200 means JPY 4200.
    const money = amount === null ? "—" : format
      ? format.format(amount / 10 ** (format.resolvedOptions().maximumFractionDigits ?? 2))
      : this.t("decisions.minorAmount", { amount: label(amount) });
    const url = this.filed[action] || "";
    const expired = url !== "" && this.#expired(action);
    const failed = this.failed[action] || "";
    return html`<article>
      <h3>${action}</h3>
      <dl>
        <dt>${this.t("field.connector")}</dt><dd>${label(row.connector)} · ${label(row.operation)} · ${label(row.class)}</dd>
        <dt>${this.t("field.target")}</dt><dd>${label(row.target)}</dd>
        <dt>${this.t("field.amount")}</dt><dd>${money}</dd>
        <dt>${this.t("field.proposedBy")}</dt><dd>${label(row.proposed_by)}</dd>
        <dt>${this.t("field.project")}</dt><dd>${label(row.project)}${row.work_item ? html` · #${label(row.work_item)}` : nothing}</dd>
      </dl>
      ${url && !expired ? html`<p role="status">${this.t("decisions.filed")}
          <a href=${url} target="_blank" rel="noopener noreferrer">${this.t("decisions.open")}</a></p>`
        : html`<button type="button" @click=${() => this.approve(action)}
            ?disabled=${!this.api || this.filing === action}>${this.filing === action
              ? this.t("decisions.filing")
              : expired ? this.t("decisions.renew") : this.t("decisions.approve")}</button>
          <p class="muted">${expired
            ? this.t("decisions.expired") : this.t("decisions.approveHint")}</p>`}
      ${failed ? html`<p role="alert" class="critical">${this.t("decisions.failed", { code: failed })}</p>` : nothing}
    </article>`;
  }

  #request(row: Row) {
    const options = rows(row.options);
    return html`<li><article>
      <h3>${this.t("decisions.request", { id: label(row.id) })} · ${label(row.project)}</h3>
      <p class="text">${label(row.question)}</p>
      <dl>
        <dt>${this.t("field.requester")}</dt><dd>${label(row.requester)}</dd>
        <dt>${this.t("field.lot")}</dt><dd>#${label(row.lot)} ${label(row.lot_title)}</dd>
        <dt>${this.t("field.due")}</dt><dd>${this.date(row.due_ts)}${row.overdue
          ? html` · ${this.t("decisions.overdue")}` : nothing}${row.urgent
          ? html` · ${this.t("decisions.urgent")}` : nothing}</dd>
      </dl>
      ${options.length ? html`<ul>${options.map((option) => html`<li>${label(option.key)})
        ${label(option.label)}${option.key === row.recommend
          ? html` — ${this.t("decisions.recommended")}` : nothing}</li>`)}</ul>` : nothing}
      <p class="muted">${this.t("decisions.answerHint", { id: label(row.id) })}</p>
    </article></li>`;
  }

  protected content(data: Row) {
    const approvals = rows(data.approvals);
    const requests = rows(data.requests);
    const waiting = rows(data.waiting_human);
    const unknown = rows(data.unknown).concat(rows(data.interrupted));
    const prose = rows(data.prose);
    if (!approvals.length && !requests.length && !waiting.length && !unknown.length
        && !prose.length) {
      return this.empty();
    }
    return html`
      ${approvals.length ? html`<h3>${this.t("decisions.approvals")}</h3>
        <div class="cards">${approvals.map((row) => this.#action(row))}</div>` : nothing}
      ${requests.length ? html`<h3>${this.t("decisions.requests")}</h3>
        <ol>${requests.map((row) => this.#request(row))}</ol>` : nothing}
      ${waiting.length ? html`<h3>${this.t("decisions.waiting")}</h3>
        <ul>${waiting.map((row) => html`<li>
          <button class="link" @click=${() => this.open("lot", { project: row.project, lot: row.id })}>#${label(row.id)} ${label(row.title)}</button>
          · ${this.state(row.state)}${row.assignee ? html` · ${label(row.assignee)}` : nothing}</li>`)}</ul>` : nothing}
      ${unknown.length ? html`<h3>${this.t("decisions.unknown")}</h3>
        <ul>${unknown.map((row) => html`<li>${label(row.action_id)} · ${label(row.hint)}</li>`)}</ul>` : nothing}
      ${prose.length ? html`<h3>${this.t("decisions.prose")}</h3>
        <ul>${prose.map((row) => html`<li><p>${label(row.sender)} → ${label(row.recipient)}
          ${row.lot ? html` · #${label(row.lot)}` : nothing}</p>
          <p class="text">${label(row.excerpt)}</p>
          <p class="muted">${this.t("decisions.proseHint", { sender: label(row.sender) })}</p></li>`)}</ul>` : nothing}`;
  }
}

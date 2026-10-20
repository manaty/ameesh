// SPDX-License-Identifier: AGPL-3.0-only
// <ameesh-chat> (L91, study §1 "Conversation with an agent"), tier 1: no
// resident agent. The message goes through the agent's shared conversation
// (L83's `POST /agents/{a}/chat`, the protocol of `ameesh chat <agent>`):
// the server deposits it in the agent's mailbox, which wakes the agent at its
// next turn, and writes its readable copy in the thread. The answer arrives
// as mail, so through the thread: the screen shows the exchanges between the
// human and the agent in the threads where they land — the project thread a
// reply sent without a lot is written in, and each lot's thread in the lot's
// own project (the server's routing, never the selected project) — the
// agent's state and, while the last word is the human's, "waiting for the
// next turn".
// Tier 2 (live answer of a resident agent, `chat.delta`) follows L75–L83.
import { css, html, nothing } from "lit";
import { ApiError, type Client } from "../api/client";
import { idempotencyKey } from "./decision-screens";
import { ReadView, label, number, object, rows, type Row } from "./read-view";

/** The API's limit on a message (`MAX_TEXT`). */
const MAX_TEXT = 8000;
/** States in which a message waits without waking the agent. */
const ASLEEP = new Set(["paused", "stopped", "dead", "error"]);

const parties = (value: unknown): string[] => label(value).split(",").map((part) => part.trim());

export class AmeeshChat extends ReadView {
  static override properties = {
    ...ReadView.properties,
    draft: { state: true }, sending: { state: true }, failed: { state: true },
  };
  static override styles = [...ReadView.styles, css`
    textarea { display: block; width: 100%; box-sizing: border-box; font: inherit; }
  `];
  headingKey = "view.chat";
  protected override required = "agent" as const;
  /** The message being written. */
  draft = "";
  sending = false;
  /** Code of the last refused send, shown under the form. */
  failed = "";
  //: one key per message: a retry after an uncertain failure replays it, never
  //: sends it twice; a new text, or a delivered one, takes a fresh key
  #key: { text: string; agent: string; lot: number | null; client: Client; key: string } | null = null;
  //: the project of a lot, read once: the server routes a message carrying a
  //: lot to the lot's project, so the screen must read its thread there even
  //: when the selected project differs
  #lotProjects = new Map<string, string>();

  override willUpdate(changed: Map<PropertyKey, unknown>): void {
    super.willUpdate(changed);
    // a refusal belongs to the target it was made for: changing the agent or
    // the lot must not show it under the new one (the draft may stay)
    if (changed.has("agent") || changed.has("lot")) this.failed = "";
  }

  /** Where the server writes a message without a lot: the agent's project. */
  #agentProject(agent: Row): string {
    const own = label(agent.project);
    return own === "—" ? this.project : own;
  }

  /** The lot's project (the server's routing), read once; `fallback` if unreadable. */
  async #lotProject(client: Client, lot: number, fallback: string,
                    signal: AbortSignal): Promise<string> {
    const key = String(lot);
    const known = this.#lotProjects.get(key);
    if (known !== undefined) return known;
    try {
      const read = object(await client.get("/api/v1/lots/{id}", { path: { id: lot }, signal }));
      const project = label(object(read.lot).app);
      if (project !== "—" && project !== "") {
        this.#lotProjects.set(key, project);
        return project;
      }
    } catch (error) {
      if (signal.aborted) throw error;
      // an unreadable lot keeps the caller's project: the screen shows what it
      // can, and the send says the refusal
    }
    return fallback;
  }

  protected async load(client: Client, signal: AbortSignal) {
    const [me, detail, overview] = await Promise.all([
      client.get("/api/v1/me", { signal }),
      client.get("/api/v1/agents/{agent}", { path: { agent: this.agent }, signal }),
      client.get("/api/v1/projects", { signal }),
    ]);
    const current = rows(object(overview).projects).flatMap((p) => rows(p.agents))
      .find((a) => a.name === this.agent);
    const agent = { ...object(object(detail).agent), ...current };
    // the server writes a message without a lot in the agent's project: the
    // `project` attribute is not the routing, it only covers an agent with none
    const project = this.#agentProject(agent);
    const explicit = /^[1-9][0-9]{0,8}$/.test(this.lot) ? Number(this.lot) : null;
    const currentLot = Number(object(agent.lot).id);
    const lots = explicit !== null ? [explicit]
      : Number.isFinite(currentLot) && currentLot > 0 ? [currentLot] : [];
    const fallback = project === "—" ? "" : project;
    const lotProjects = await Promise.all(lots.map((lot) =>
      this.#lotProject(client, lot, fallback, signal)));
    // where messages land: the project thread (a reply sent without a lot) and
    // each lot's thread in its own project; never the selected project alone
    const reads = new Map<string, { project: string; lot: string | null }>();
    if (fallback) reads.set(fallback + "|", { project: fallback, lot: null });
    lots.forEach((lot, index) => {
      const where = lotProjects[index] || fallback;
      if (where) reads.set(where + "|" + lot, { project: where, lot: String(lot) });
    });
    const threads = await Promise.all([...reads.values()].map(({ project: where, lot }) => {
      const read = lot === null
        ? client.get("/api/v1/threads/{project}", { path: { project: where }, signal })
        : client.get("/api/v1/threads/{project}/{lot}", { path: { project: where, lot }, signal });
      // a thread not written yet is an empty conversation, not an error
      return read.catch((error) => {
        if (error instanceof ApiError && error.status === 404) return { entries: [] };
        throw error;
      });
    }));
    return { me: label(object(me).human), agent, entries: threads.flatMap((t) => rows(object(t).entries)) };
  }

  /** The exchanges between the human and the agent, oldest first, once each. */
  #exchanges(data: Row): Row[] {
    const me = label(data.me);
    const mine = new Set(me === "—" ? [] : [me, "agent:" + me.replace(/^human:/, "")]);
    const agent = "agent:" + this.agent;
    const seen = new Set<string>();
    return rows(data.entries).filter((entry) => {
      const to = parties(entry.recipient);
      // Log.drain_threads writes final answers without recipients; its
      // conversation metadata identifies the agent on that shared thread.
      const final = entry.recipient === ""
        && object(object(entry.meta).conversation).agent === this.agent;
      const between = (entry.author === agent && (final || to.some((party) => mine.has(party))))
        || (mine.has(label(entry.author)) && to.includes(agent));
      const id = label(entry.id) + "|" + label(entry.ts);
      if (!between || seen.has(id)) return false;
      seen.add(id);
      return true;
    }).sort((a, b) => (number(a.ts) ?? 0) - (number(b.ts) ?? 0));
  }

  /** Send the draft; the agent reads it at its next turn. */
  async send(): Promise<void> {
    const client = this.api;
    const text = this.draft.trim();
    if (!client || !text || this.sending || !this.agent) return;
    const lot = /^[1-9][0-9]{0,8}$/.test(this.lot) ? Number(this.lot) : null;
    if (this.#key?.text !== text || this.#key.agent !== this.agent
      || this.#key.lot !== lot || this.#key.client !== client) {
      this.#key = { text, agent: this.agent, lot, client, key: idempotencyKey() };
    }
    this.sending = true; this.failed = "";
    try {
      await client.post("/api/v1/agents/{agent}/chat", {
        path: { agent: this.agent },
        body: lot === null ? { text } : { text, lot },
        headers: { "Idempotency-Key": this.#key.key },
      });
      this.#key = null;
      if (this.draft.trim() === text) this.draft = "";
      await this.refresh();
    } catch (error) {
      this.failed = error instanceof ApiError ? error.code : "network";
      // The API stores a refusal under its key and replays it: keeping that key
      // would refuse the same message again after the cause is fixed. Keep it
      // only while the outcome is unknown (network, 5xx, first request still
      // running); a deterministic answer releases it.
      if (error instanceof ApiError && error.status < 500
          && error.code !== "idempotency_in_progress") {
        this.#key = null;
      }
    } finally {
      this.sending = false;
    }
  }

  #wake(agent: Row, last: Row | undefined, me: string) {
    const state = label(agent.state ?? agent.status);
    const waiting = last !== undefined && label(last.author) !== "agent:" + this.agent && me !== "—";
    return html`<p role="status">${this.t("chat.agentState", { agent: this.agent, state: this.state(state) })}
      ${agent.unread ? html` · ${this.t("chat.unread", { count: label(agent.unread),
        n: number(agent.unread) ?? 0 })}` : nothing}</p>
      ${waiting ? html`<p class="muted">${ASLEEP.has(state)
        ? this.t("chat.asleep", { state: this.state(state) }) : this.t("chat.waiting")}</p>` : nothing}`;
  }

  protected content(data: Row) {
    const agent = object(data.agent);
    const entries = this.#exchanges(data);
    const me = label(data.me);
    return html`
      ${this.#wake(agent, entries[entries.length - 1], me)}
      ${entries.length ? html`<ol>${entries.map((entry) => html`<li><article>
        <h3>${label(entry.author)}</h3>
        <p class="muted">${this.date(entry.ts)}</p><div class="text">${label(entry.text)}</div>
      </article></li>`)}</ol>` : html`<p class="muted">${this.t("chat.empty")}</p>`}
      <form @submit=${(event: SubmitEvent) => { event.preventDefault(); void this.send(); }}>
        <p><label for="message">${this.t("chat.message", { agent: this.agent })}</label></p>
        <textarea id="message" rows="4" maxlength=${MAX_TEXT} .value=${this.draft}
          @input=${(event: Event) => { this.draft = (event.target as HTMLTextAreaElement).value; }}
          ?disabled=${this.sending}></textarea>
        <p><button type="submit" ?disabled=${!this.api || this.sending || !this.draft.trim()}>${this.sending
          ? this.t("chat.sending") : this.t("chat.send")}</button>
          <span class="muted">${this.t("chat.hint")}</span></p>
        ${this.failed ? html`<p role="alert" class="critical">${this.t("chat.failed", { code: this.failed })}</p>` : nothing}
      </form>`;
  }
}

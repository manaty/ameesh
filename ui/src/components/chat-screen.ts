// SPDX-License-Identifier: AGPL-3.0-only
// <ameesh-chat> (L91, study §1 "Conversation with an agent"), tier 1: no
// resident agent. The message goes through the agent's shared conversation
// (L83's `POST /agents/{a}/chat`, the protocol of `ameesh chat <agent>`):
// the server deposits it in the agent's mailbox, which wakes the agent at its
// next turn, and writes its readable copy in the thread. The answer arrives
// as mail, so through the thread: the screen shows the exchanges between the
// human and the agent in the threads where they land (the lot's when one is
// set; otherwise the agent's project and its current lot), the agent's state
// and, while the last word is the human's, "waiting for the next turn".
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

  protected async load(client: Client, signal: AbortSignal) {
    const [me, detail, overview] = await Promise.all([
      client.get("/api/v1/me", { signal }),
      client.get("/api/v1/agents/{agent}", { path: { agent: this.agent }, signal }),
      client.get("/api/v1/projects", { signal }),
    ]);
    const current = rows(object(overview).projects).flatMap((p) => rows(p.agents))
      .find((a) => a.name === this.agent);
    const agent = { ...object(object(detail).agent), ...current };
    const project = this.project || label(agent.project);
    const lots = this.lot ? [this.lot] : [null, object(agent.lot).id ?? null];
    const threads = project === "—" ? [] : await Promise.all(
      [...new Set(lots.map((lot) => lot === null ? null : String(lot)))].map((lot) => {
        const read = lot === null
          ? client.get("/api/v1/threads/{project}", { path: { project }, signal })
          : client.get("/api/v1/threads/{project}/{lot}", { path: { project, lot }, signal });
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
    } finally {
      this.sending = false;
    }
  }

  #wake(agent: Row, last: Row | undefined, me: string) {
    const state = label(agent.state ?? agent.status);
    const waiting = last !== undefined && label(last.author) !== "agent:" + this.agent && me !== "—";
    return html`<p role="status">${this.t("chat.agentState", { agent: this.agent, state: this.state(state) })}
      ${agent.unread ? html` · ${this.t("chat.unread", { count: label(agent.unread) })}` : nothing}</p>
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

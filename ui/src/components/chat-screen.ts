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
/** States in which a message waits in the mailbox without waking the agent:
 * the send is accepted, a turn opens once the cause is lifted. */
const ASLEEP = new Set(["paused", "error"]);
/** Raw registry statuses that refuse the message instead of queueing it
 * (`undeliverable.check_send`, without `--queue`): a stopped agent never
 * receives it, the human must resume it first. `dead` is apart: its expired
 * lease is taken back by the executor, so the message does wait in its box. */
const REFUSED = new Set(["stopped"]);

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
    // a stopped agent refuses the message (409 refused): the form is disabled,
    // and a direct call must not pretend to queue anything either
    if (REFUSED.has(label(object(object(this.data).agent).status))) return;
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
    const status = label(agent.status);
    const state = label(agent.state ?? status);
    // `progress` merges `dead` (expired lease) into `stopped`, but the API
    // still queues the message there and the executor takes the agent back by
    // itself: show it apart from a manual stop, which only a human resumes.
    const offline = state === "dead" || (state === "stopped" && !REFUSED.has(status));
    const shown = offline ? "dead" : state;
    const waiting = last !== undefined && label(last.author) !== "agent:" + this.agent && me !== "—";
    const stopped = REFUSED.has(status);
    const notice = stopped
      ? this.t("chat.stopped", { state: this.state(shown), agent: this.agent })
      : offline
        ? this.t("chat.dead", { state: this.state(shown) })
        : ASLEEP.has(shown)
          ? this.t("chat.asleep", { state: this.state(shown) })
          : this.t("chat.waiting");
    return html`<p role="status">${this.t("chat.agentState", { agent: this.agent, state: this.state(shown) })}
      ${agent.unread ? html` · ${this.t("chat.unread", { count: label(agent.unread) })}` : nothing}</p>
      ${waiting || stopped ? html`<p class="muted">${notice}</p>` : nothing}`;
  }

  protected content(data: Row) {
    const agent = object(data.agent);
    // the form is disabled for a stopped agent: the API would refuse the send
    const refused = REFUSED.has(label(agent.status));
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
          ?disabled=${this.sending || refused}></textarea>
        <p><button type="submit" ?disabled=${!this.api || this.sending || refused || !this.draft.trim()}>${this.sending
          ? this.t("chat.sending") : this.t("chat.send")}</button>
          ${refused ? nothing : html`<span class="muted">${this.t("chat.hint")}</span>`}</p>
        ${this.failed ? html`<p role="alert" class="critical">${this.t("chat.failed", { code: this.failed })}</p>` : nothing}
      </form>`;
  }
}

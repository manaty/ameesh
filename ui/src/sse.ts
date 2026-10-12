// SPDX-License-Identifier: AGPL-3.0-only
// Server-sent events over fetch (study §2.1): EventSource cannot send the
// bearer token, so the stream is read by hand, with `Last-Event-ID` on each
// reconnection. The parser follows the HTML event-stream format.
import type { Client } from "./api/client";

export interface StreamEvent {
  /** `event:` field, `message` by default. */
  type: string;
  data: string;
  /** Last `id:` seen, carried over to the events that follow (HTML spec). */
  id: string;
}

/** Incremental parser: feed it decoded text, get the complete events. */
export class EventStreamParser {
  private buffer = "";
  private data: string[] = [];
  private type = "";
  private sawCR = false;
  lastId = "";
  /** Last `retry:` value, in milliseconds. */
  retry: number | null = null;

  push(chunk: string): StreamEvent[] {
    const events: StreamEvent[] = [];
    let text = chunk;
    // a CR LF split across two chunks is one line end
    if (this.sawCR && text.startsWith("\n")) text = text.slice(1);
    this.sawCR = text.endsWith("\r");
    this.buffer += text;
    const lines = this.buffer.split(/\r\n|\r|\n/);
    this.buffer = lines.pop() ?? "";
    for (const line of lines) {
      const event = this.line(line);
      if (event) events.push(event);
    }
    return events;
  }

  private line(line: string): StreamEvent | null {
    if (line === "") {
      if (!this.data.length) {
        this.type = "";
        return null;
      }
      const event = { type: this.type || "message", data: this.data.join("\n"), id: this.lastId };
      this.data = [];
      this.type = "";
      return event;
    }
    if (line.startsWith(":")) return null; // comment: heartbeat
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.data.push(value);
    else if (field === "event") this.type = value;
    else if (field === "id" && !value.includes("\0")) this.lastId = value;
    else if (field === "retry" && /^[0-9]+$/.test(value)) this.retry = Number(value);
    return null;
  }
}

export interface StreamOptions {
  onEvent(event: StreamEvent): void;
  /** Connected (true) or waiting to reconnect (false). */
  onState?(connected: boolean): void;
  /** Gave up: a refusal that retrying will not change (401, 403, 404…). */
  onError?(error: { status: number; message: string }): void;
  /** Resume after this id (`Last-Event-ID`). */
  lastEventId?: string;
  /** First delay before reconnecting, in ms; doubled up to `maxDelay`. */
  delay?: number;
  maxDelay?: number;
  signal?: AbortSignal;
}

const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);

/** Reads `path` of the mesh until `signal` aborts; returns when stopped. */
export async function readStream(client: Client, path: string, options: StreamOptions): Promise<void> {
  const base = options.delay ?? 1000;
  const max = options.maxDelay ?? 30000;
  let lastId = options.lastEventId ?? "";
  let wait = base;
  let serverRetry: number | null = null;
  while (!options.signal?.aborted) {
    const parser = new EventStreamParser();
    parser.lastId = lastId;
    try {
      const headers: Record<string, string> = { Accept: "text/event-stream" };
      if (lastId) headers["Last-Event-ID"] = lastId;
      const response = await client.request(path, { headers, signal: options.signal });
      if (!response.ok || !response.body) {
        if (!RETRYABLE.has(response.status)) {
          options.onError?.({ status: response.status, message: response.statusText });
          return;
        }
      } else {
        options.onState?.(true);
        wait = base;
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const event of parser.push(value)) options.onEvent(event);
          lastId = parser.lastId;
          serverRetry = parser.retry ?? serverRetry;
        }
      }
    } catch (error) {
      if (options.signal?.aborted) return;
      // network error: reconnect below
    }
    if (options.signal?.aborted) return;
    options.onState?.(false);
    await sleep(serverRetry ?? wait, options.signal);
    wait = Math.min(wait * 2, max);
  }
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}

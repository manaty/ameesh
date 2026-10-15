// SPDX-License-Identifier: AGPL-3.0-only
// One stream per client, shared by all mounted read screens.
import type { Client } from "./api/client";
import { readStream, type StreamEvent } from "./sse";

export type LiveState = "waiting" | "connected" | "unavailable";
type Listener = { event(event: StreamEvent): void; state(state: LiveState): void; error(status: number): void };
type Hub = { abort: AbortController; listeners: Set<Listener>; state: LiveState };
const hubs = new WeakMap<Client, Hub>();

export function subscribe(client: Client, listener: Listener): () => void {
  let hub = hubs.get(client);
  if (!hub) {
    hub = { abort: new AbortController(), listeners: new Set(), state: "waiting" };
    hubs.set(client, hub);
    const current = hub;
    const state = (value: LiveState) => {
      current.state = value;
      for (const item of current.listeners) item.state(value);
    };
    // Re-read on every connection too: events while no view was mounted,
    // or while connecting for the first time, must not leave a stale snapshot.
    void readStream(client, "/api/v1/events", {
      signal: current.abort.signal,
      onEvent: (event) => { for (const item of current.listeners) item.event(event); },
      onState: (connected) => {
        state(connected ? "connected" : "waiting");
        if (connected) for (const item of current.listeners) item.event({ type: "resync", id: "", data: "" });
      },
      onError: (error) => {
        state("unavailable");
        for (const item of current.listeners) item.error(error.status);
      },
    });
  }
  hub.listeners.add(listener);
  listener.state(hub.state);
  const current = hub;
  return () => {
    current.listeners.delete(listener);
    if (!current.listeners.size) {
      // Lit replaces a view synchronously: let its successor join this
      // stream before closing it, so navigation does not fill the server's
      // stream slots with connections waiting for their next heartbeat.
      queueMicrotask(() => {
        if (!current.listeners.size && hubs.get(client) === current) {
          current.abort.abort();
          hubs.delete(client);
        }
      });
    }
  };
}

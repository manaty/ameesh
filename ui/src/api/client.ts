// SPDX-License-Identifier: AGPL-3.0-only
// API client of one mesh (study §2.2): typed from the OpenAPI document,
// the token asked of the host at each request and never kept.
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

type Get<P extends keyof paths> = paths[P] extends { get: infer O } ? O : never;
/** Paths answered by GET (all of them, the API being read-only for now). */
export type ReadPath = { [P in keyof paths]: Get<P> extends never ? never : P }[keyof paths];
type Parameters<P extends ReadPath> = Get<P> extends { parameters: infer A } ? A : never;
type PathParams<P extends ReadPath> = Parameters<P> extends { path: infer T } ? T : never;
type QueryParams<P extends ReadPath> = Parameters<P> extends { query?: infer T } ? T : never;
/** The 200 body of a path. */
export type Body<P extends ReadPath> = Get<P> extends {
  responses: { 200: { content: { "application/json": infer B } } };
} ? B : never;

export type RequestOptions<P extends ReadPath> =
  ([PathParams<P>] extends [never] ? { path?: never } : { path: PathParams<P> })
  & { query?: QueryParams<P>; signal?: AbortSignal };

/** The token of the current human, asked for each request; null: none. */
export type TokenProvider = () => string | null | Promise<string | null>;

export interface ClientOptions {
  /** Origin (and prefix) of the mesh, e.g. `http://127.0.0.1:8780`. */
  baseUrl: string;
  tokenProvider?: TokenProvider;
  /** For tests and hosts that wrap fetch; the global one otherwise. */
  fetch?: typeof fetch;
  /** Told of every 401, with the token that was refused (null: none was
   *  sent), whoever made the request: page, component or stream. */
  onUnauthorized?: (token: string | null) => void;
}

/** A refused request: the `ameesh-error/1` body when there is one. */
export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export interface Client {
  readonly baseUrl: string;
  get<P extends ReadPath>(path: P, ...options: RequestOptions<P> extends { path: unknown }
    ? [RequestOptions<P>] : [RequestOptions<P>?]): Promise<Body<P>>;
  /** A URL of the mesh and the headers to call it with (used by the stream). */
  request(path: string, init?: RequestInit): Promise<Response>;
}

export function createClient(options: ClientOptions): Client {
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  const origin = new URL(baseUrl).origin;
  const prefix = new URL(baseUrl).pathname.replace(/\/+$/, "");
  const doFetch = options.fetch ?? ((input, init) => fetch(input, init));

  /** A path of this mesh, never another origin: the token goes with it. */
  function resolve(path: string): string {
    if (!path.startsWith("/") || path.startsWith("//")) {
      throw new TypeError("path must start with a single '/': " + path);
    }
    const url = new URL(prefix + path, origin);
    if (url.origin !== origin) throw new TypeError("path leaves the mesh: " + path);
    return url.href;
  }

  async function request(path: string, init: RequestInit = {}): Promise<Response> {
    const url = resolve(path);
    const headers = new Headers(init.headers);
    headers.set("Accept", headers.get("Accept") ?? "application/json");
    const token = options.tokenProvider ? await options.tokenProvider() : null;
    if (token) headers.set("Authorization", "Bearer " + token);
    const response = await doFetch(url, {
      ...init, headers, credentials: "omit", cache: "no-store", redirect: "error",
    });
    if (response.status === 401) options.onUnauthorized?.(token || null);
    return response;
  }

  async function get(path: string, opts: {
    path?: Record<string, string | number>; query?: Record<string, unknown>; signal?: AbortSignal;
  } = {}): Promise<unknown> {
    const url = expand(path, opts.path ?? {}) + queryString(opts.query ?? {});
    const response = await request(url, { method: "GET", signal: opts.signal });
    const body = await response.json().catch(() => null) as
      { error?: unknown; message?: unknown } | null;
    if (!response.ok) {
      throw new ApiError(response.status,
        typeof body?.error === "string" ? body.error : "http_" + response.status,
        typeof body?.message === "string" ? body.message : response.statusText);
    }
    return body;
  }

  return { baseUrl, get: get as Client["get"], request };
}

/** `/lots/{id}` with `{id: 7}` → `/lots/7`, each value encoded. */
export function expand(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{([^}]+)\}/g, (_, name: string) => {
    if (!(name in params)) throw new TypeError("missing path parameter: " + name);
    return encodeURIComponent(String(params[name]));
  });
}

function queryString(query: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === false) continue;
    search.set(name, value === true ? "1" : String(value));
  }
  const text = search.toString();
  return text ? "?" + text : "";
}

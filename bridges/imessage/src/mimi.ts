// A small client for the Mimi agent API (docs/API.md): POST /v1/turns as a
// shop, POST /v1/media for attachments (demo mode), GET /v1/turns/{id} while a
// turn runs, GET /v1/threads/{id} for replies released later by an owner's
// approval, and a voice note read back for a reply.

import type { Logger } from "./log.ts";
import type { State } from "./state.ts";

export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export interface MediaRef {
  kind: "image" | "voice" | "document";
  ref: string;
  mime: string;
  sha256: string;
  caption?: string;
}

export interface TurnBody {
  message_id: string;
  source: "shop_message";
  phone: string;
  text: string;
  media?: MediaRef;
  received_at: string;
}

export interface Reply {
  message_id: string;
  channel: string;
  shop_id: string;
  text: string;
  kind: string;
  status: string;
  voice?: unknown;
}

export interface Turn {
  turn_id: string;
  thread_id: string;
  state: string;
  status: string;
  duplicate: boolean;
  replies: Reply[];
}

export interface ThreadMessage {
  message_id: string;
  direction: string;
  channel: string;
  shop_id: string;
  text: string;
  status: string;
  at: string;
  voice?: boolean;
}

export interface DemoLimit {
  code: string;
  message: string;
  resets_at: string;
}

/** The demo refused (429 cap, 503 paused). Never retried. */
export class DemoLimited extends Error {
  readonly limit: DemoLimit;
  readonly status: number;
  constructor(status: number, limit: DemoLimit) {
    super(`demo limit: ${limit.code}`);
    this.status = status;
    this.limit = limit;
  }
}

/** A 4xx other than 401/429: our request is wrong, retrying will not help. */
export class ApiRefused extends Error {
  readonly status: number;
  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.status = status;
  }
}

export interface ClientOptions {
  base: string;
  /** Fixed bearer; empty means open (and renew) our own demo session. */
  token: string;
  fetch?: FetchLike;
  log: Logger;
  state: State;
  timeoutMs: number;
  turnTimeoutMs: number;
  retries?: number;
  backoffMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

const FINISHED = new Set(["done", "failed"]);

export class MimiClient {
  private readonly o: Required<Omit<ClientOptions, "token">> & { token: string };

  constructor(opts: ClientOptions) {
    this.o = {
      fetch: (u, i) => fetch(u, i),
      retries: 3,
      backoffMs: 500,
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      ...opts,
    };
  }

  get tenantId(): string {
    return this.o.state.data.session?.tenantId ?? "";
  }

  /** The bearer to use: the fixed token, or our own demo session (opened on first use). */
  private async bearer(): Promise<string> {
    if (this.o.token) return this.o.token;
    const s = this.o.state.data.session;
    if (s && Date.parse(s.expiresAt) - Date.now() > 60_000) return s.token;
    return this.openSession();
  }

  async openSession(): Promise<string> {
    const res = await this.raw("POST", "/v1/demo/sessions", { auth: "" });
    const body = (await res.json()) as { token: string; tenant_id: string; expires_at: string };
    this.o.state.data.session = { token: body.token, tenantId: body.tenant_id, expiresAt: body.expires_at };
    this.o.state.save();
    this.o.log.info("demo_session_opened", { tenant: body.tenant_id, expires_at: body.expires_at });
    // A renewal is a fresh Coral Lane copy (a new tenant): history resets and each
    // one counts against the per-IP daily session cap. For long runs set MIMI_TOKEN.
    if (Object.keys(this.o.state.data.shops).length) {
      this.o.log.warn("demo_session_new_copy", { tenant: body.tenant_id });
      this.o.state.data.shops = {};
      this.o.state.save();
    }
    return body.token;
  }

  /** Learns the tenant for a fixed token (thread ids are "<tenant>:shop:<shop_id>"). */
  async me(): Promise<{ tenant_id: string; role: string; mode: string }> {
    const res = await this.call("GET", "/v1/me");
    const me = (await res.json()) as { tenant_id: string; role: string; mode: string };
    if (this.o.token && !this.o.state.data.session) {
      this.o.state.data.session = { token: "", tenantId: me.tenant_id, expiresAt: "" };
    } else if (this.o.state.data.session) {
      this.o.state.data.session.tenantId = me.tenant_id;
    }
    return me;
  }

  async upload(bytes: Uint8Array, mime: string): Promise<MediaRef> {
    const res = await this.call("POST", "/v1/media", { body: bytes, contentType: mime });
    const m = (await res.json()) as MediaRef;
    return { kind: m.kind, ref: m.ref, mime: m.mime, sha256: m.sha256 };
  }

  /** Posts the turn and waits until it is finished (polling while the API answers 202). */
  async postTurn(body: TurnBody): Promise<Turn> {
    const res = await this.call("POST", "/v1/turns?wait=true", { json: body, timeoutMs: this.o.turnTimeoutMs });
    let turn = (await res.json()) as Turn;
    const deadline = Date.now() + this.o.turnTimeoutMs;
    let wait = 1000;
    while (!FINISHED.has(turn.state) && Date.now() < deadline) {
      await this.o.sleep(wait);
      wait = Math.min(wait * 2, 5000);
      turn = (await (await this.call("GET", `/v1/turns/${encodeURIComponent(turn.turn_id)}`)).json()) as Turn;
    }
    return turn;
  }

  async thread(threadId: string): Promise<ThreadMessage[]> {
    const res = await this.call("GET", `/v1/threads/${encodeURIComponent(threadId)}`);
    return ((await res.json()) as { messages: ThreadMessage[] }).messages;
  }

  async voice(messageId: string): Promise<{ bytes: Uint8Array; mime: string }> {
    const res = await this.call("GET", `/v1/messages/${encodeURIComponent(messageId)}/voice`);
    return { bytes: new Uint8Array(await res.arrayBuffer()), mime: res.headers.get("content-type") ?? "audio/ogg" };
  }

  /** An authenticated call with retries on network errors, timeouts and 5xx; one session renewal on 401. */
  private async call(
    method: string,
    path: string,
    opt: { json?: unknown; body?: Uint8Array; contentType?: string; timeoutMs?: number } = {},
  ): Promise<Response> {
    let renewed = false;
    for (;;) {
      const auth = await this.bearer();
      try {
        return await this.raw(method, path, { ...opt, auth });
      } catch (e) {
        if (e instanceof ApiRefused && e.status === 401 && !this.o.token && !renewed) {
          renewed = true;
          this.o.state.data.session = undefined;
          this.o.log.warn("demo_session_expired_renewing");
          continue;
        }
        throw e;
      }
    }
  }

  private async raw(
    method: string,
    path: string,
    opt: { json?: unknown; body?: Uint8Array; contentType?: string; timeoutMs?: number; auth: string },
  ): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (opt.auth) headers.Authorization = `Bearer ${opt.auth}`;
    let body: BodyInit | undefined;
    if (opt.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opt.json);
    } else if (opt.body) {
      headers["Content-Type"] = opt.contentType ?? "application/octet-stream";
      body = opt.body as unknown as BodyInit;
    }
    let lastErr: unknown;
    for (let attempt = 0; attempt <= this.o.retries; attempt++) {
      if (attempt > 0) await this.o.sleep(this.o.backoffMs * 2 ** (attempt - 1));
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), opt.timeoutMs ?? this.o.timeoutMs);
      let res: Response;
      try {
        const got = await this.o.fetch(`${this.o.base}${path}`, { method, headers, body, signal: ctl.signal });
        // Read the body under the same timer: a response that stalls mid-body
        // must time out too, or it wedges the sender's chain.
        const buf = await got.arrayBuffer();
        const empty = buf.byteLength === 0 || [101, 204, 205, 304].includes(got.status);
        res = new Response(empty ? null : buf, { status: got.status, statusText: got.statusText, headers: got.headers });
      } catch (e) {
        lastErr = e;
        this.o.log.warn("api_retry", { path: pathOnly(path), attempt, reason: (e as Error).name || "network" });
        continue;
      } finally {
        clearTimeout(timer);
      }
      if (res.ok) return res;
      if (res.status === 429 || res.status === 503) {
        const j = (await res.json().catch(() => ({}))) as { detail?: string; demo_limit?: DemoLimit };
        if (j.demo_limit) throw new DemoLimited(res.status, j.demo_limit);
        if (res.status === 503) {
          lastErr = new ApiRefused(503, j.detail ?? "unavailable");
          continue;
        }
        throw new ApiRefused(429, j.detail ?? "too many requests");
      }
      if (res.status >= 500) {
        lastErr = new ApiRefused(res.status, "server error");
        this.o.log.warn("api_retry", { path: pathOnly(path), attempt, status: res.status });
        continue;
      }
      const j = (await res.json().catch(() => ({}))) as { detail?: unknown };
      throw new ApiRefused(res.status, typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? ""));
    }
    throw lastErr instanceof Error ? lastErr : new Error("API unreachable");
  }
}

function pathOnly(p: string): string {
  return p.split("?")[0]?.replace(/\/(turn|msg|apr)_[^/]+/g, "/:id") ?? p;
}

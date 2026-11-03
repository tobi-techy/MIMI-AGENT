// Test doubles: a fake chat transport, a scripted Mimi API and a fake converter.

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Bridge } from "../src/bridge.ts";
import { type Limits, type Settings, loadShops, parseSenders } from "../src/config.ts";
import { memoryLogger } from "../src/log.ts";
import type { Converter, Target } from "../src/media.ts";
import { MimiClient } from "../src/mimi.ts";
import { State } from "../src/state.ts";
import type { InboundMessage, Outbound, Part } from "../src/types.ts";

export const ALICE = "+1 (555) 010-2000";
export const BOB = "bob@example.com";
export const STRANGER = "+15550109999";
export const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);
export const CAF = new Uint8Array([0x63, 0x61, 0x66, 0x66, 0, 1, 0, 0]);
export const OGG = new Uint8Array([0x4f, 0x67, 0x67, 0x53, 0, 2, 0, 0]);
export const M4A = new Uint8Array([0, 0, 0, 0x20, 0x66, 0x74, 0x79, 0x70, 0x4d, 0x34, 0x41, 0x20]);
export const JPG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 0]);
export const HEIC = new Uint8Array([0, 0, 0, 0x18, 0x66, 0x74, 0x79, 0x70, 0x68, 0x65, 0x69, 0x63]);

export class FakeConversation {
  readonly id: string;
  readonly sent: Outbound[] = [];
  failNext = false;
  constructor(id = "any;-;+15550102000") {
    this.id = id;
  }
  send = async (out: Outbound): Promise<void> => {
    if (this.failNext) {
      this.failNext = false;
      throw new Error("send failed");
    }
    this.sent.push(out);
  };
  get texts(): string[] {
    return this.sent.flatMap((o) => (o.kind === "text" ? [o.text] : []));
  }
}

let seq = 0;
export function inbound(
  over: Partial<Omit<InboundMessage, "conversation">> & { conversation?: FakeConversation; text?: string } = {},
): InboundMessage & { conversation: FakeConversation } {
  seq += 1;
  const { text, conversation, ...rest } = over;
  const parts: Part[] = rest.parts ?? (text === undefined ? [{ kind: "text", text: "hello" }] : [{ kind: "text", text }]);
  return {
    id: `guid-${seq}`,
    platform: "imessage",
    sender: ALICE,
    isGroup: false,
    outbound: false,
    at: new Date("2026-11-03T12:00:00Z"),
    ...rest,
    parts,
    conversation: conversation ?? new FakeConversation(),
  };
}

export interface Call {
  method: string;
  path: string;
  auth: string;
  contentType: string;
  json: unknown;
  bytes?: Uint8Array;
}

type Handler = (call: Call) => Response | Promise<Response> | undefined;

/** A scripted Mimi API: handlers run in order; the first to answer wins. */
export class FakeApi {
  readonly calls: Call[] = [];
  readonly handlers: Handler[] = [];
  base = "https://mimi.test";

  on(method: string, prefix: string, h: (call: Call) => Response | Promise<Response>): this {
    this.handlers.push((c) => (c.method === method && c.path.startsWith(prefix) ? h(c) : undefined));
    return this;
  }

  fetch = async (url: string, init: RequestInit = {}): Promise<Response> => {
    const headers = (init.headers ?? {}) as Record<string, string>;
    const path = url.slice(this.base.length);
    const call: Call = {
      method: init.method ?? "GET",
      path,
      auth: headers.Authorization ?? "",
      contentType: headers["Content-Type"] ?? "",
      json: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
      bytes: init.body instanceof Uint8Array ? init.body : undefined,
    };
    this.calls.push(call);
    for (const h of this.handlers) {
      const r = await h(call);
      if (r) return r;
    }
    return json(404, { detail: "not found" });
  };

  turns(): Call[] {
    return this.calls.filter((c) => c.method === "POST" && c.path.startsWith("/v1/turns"));
  }
}

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

export function reply(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    message_id: `out:${Math.random().toString(36).slice(2)}`,
    channel: "shop_whatsapp",
    shop_id: "shop-001",
    text: "Thank you. Your delivery is confirmed.",
    kind: "reply",
    status: "ready",
    at: "2026-11-03T12:00:05+00:00",
    ...over,
  };
}

export function turn(replies: Record<string, unknown>[], over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    turn_id: "turn_1",
    thread_id: "demo-abc:shop:shop-001",
    state: "done",
    status: "done",
    duplicate: false,
    replies,
    ...over,
  };
}

export class FakeConverter implements Converter {
  readonly calls: Target[] = [];
  fail = false;
  async convert(_bytes: Uint8Array, target: Target): Promise<Uint8Array> {
    this.calls.push(target);
    if (this.fail) throw new Error("ffmpeg failed");
    return target === "ogg" ? OGG : target === "m4a" ? M4A : JPG;
  }
}

export const LIMITS: Limits = { perSenderPerMinute: 6, perSenderPerDay: 60, dailySendCap: 4500, newSendersPerDay: 45 };

export interface Rig {
  api: FakeApi;
  client: MimiClient;
  bridge: Bridge;
  state: State;
  log: ReturnType<typeof memoryLogger>;
  conv: FakeConverter;
  dir: string;
  settings: Pick<Settings, "senders" | "pauseFile" | "pausedEnv" | "limits" | "pollApprovals" | "pollEveryMs">;
}

export function rig(opts: { token?: string; dir?: string; limits?: Partial<Limits>; pausedEnv?: boolean; api?: FakeApi; timeoutMs?: number; turnTimeoutMs?: number } = {}): Rig {
  const dir = opts.dir ?? mkdtempSync(join(tmpdir(), "bridge-test-"));
  const api = opts.api ?? new FakeApi();
  const log = memoryLogger();
  const state = new State(join(dir, "state.json"));
  const settings = {
    senders: parseSenders(
      { senders: [{ handle: ALICE, shop_id: "shop-001" }, { handle: BOB, shop_id: "shop-002" }] },
      loadShops(),
    ),
    pauseFile: join(dir, "PAUSE"),
    pausedEnv: opts.pausedEnv ?? false,
    limits: { ...LIMITS, ...opts.limits },
    pollApprovals: false,
    pollEveryMs: 10_000,
  };
  const client = new MimiClient({
    base: api.base,
    token: opts.token ?? "test-bearer",
    fetch: api.fetch,
    log,
    state,
    timeoutMs: opts.timeoutMs ?? 1000,
    turnTimeoutMs: opts.turnTimeoutMs ?? 2000,
    backoffMs: 1,
    sleep: async () => undefined,
  });
  const conv = new FakeConverter();
  const bridge = new Bridge({ settings, api: client, state, log, converter: conv });
  return { api, client, bridge, state, log, conv, dir, settings };
}

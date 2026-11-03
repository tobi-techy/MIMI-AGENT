// One fake inbound message through the real bridge and the real Mimi API, no
// iMessage involved: proves the request shape and shows Mimi's reply.
//
//   node --env-file-if-exists=.env src/smoke.ts "Good morning, how much do I owe?"
//
// Uses the first sender in bridge.config.json. Costs one demo turn (a few naira).

import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { Bridge } from "./bridge.ts";
import { loadSettings } from "./config.ts";
import { makeLogger } from "./log.ts";
import { ffmpegConverter } from "./media.ts";
import { MimiClient } from "./mimi.ts";
import { State } from "./state.ts";
import type { Outbound } from "./types.ts";

const settings = loadSettings();
const log = makeLogger(settings.logBodies);
mkdirSync(settings.stateDir, { recursive: true });
// Its own state file and demo session, never the running bridge's live tenant.
const state = new State(join(settings.stateDir, "smoke-state.json"));
const api = new MimiClient({ base: settings.apiBase, token: settings.token, log, state, timeoutMs: settings.httpTimeoutMs, turnTimeoutMs: settings.turnTimeoutMs });
await api.me();
const bridge = new Bridge({ settings, api, state, log, converter: ffmpegConverter(settings.ffmpeg) });
const [first] = settings.senders.values();
if (!first) throw new Error("no sender in the allowlist");
const replies: string[] = [];
const outcome = await bridge.handle({
  id: `smoke-${Date.now()}`,
  platform: "smoke",
  sender: first.key,
  isGroup: false,
  outbound: false,
  at: new Date(),
  parts: [{ kind: "text", text: process.argv[2] || "Good morning" }],
  conversation: {
    id: "smoke",
    send: async (out: Outbound) => {
      replies.push(out.kind === "text" ? out.text : `[${out.kind}]`);
    },
  },
});
console.log(JSON.stringify({ outcome, shop: first.shopId, tenant: api.tenantId, replies }, null, 2));

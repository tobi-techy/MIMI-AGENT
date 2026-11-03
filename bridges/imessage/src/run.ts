// Shared start-up for every entrypoint: settings, state, the API client and
// the bridge. The entrypoint brings its own Transport (cloud or local).

import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { Bridge } from "./bridge.ts";
import { loadSettings } from "./config.ts";
import { makeLogger } from "./log.ts";
import { ffmpegConverter } from "./media.ts";
import { MimiClient } from "./mimi.ts";
import { State } from "./state.ts";
import type { Transport } from "./types.ts";

export async function start(makeTransport: () => Promise<Transport>): Promise<void> {
  const settings = loadSettings();
  const log = makeLogger(settings.logBodies);
  mkdirSync(settings.stateDir, { recursive: true });
  const state = new State(join(settings.stateDir, "bridge-state.json"));
  const api = new MimiClient({
    base: settings.apiBase,
    token: settings.token,
    log,
    state,
    timeoutMs: settings.httpTimeoutMs,
    turnTimeoutMs: settings.turnTimeoutMs,
  });
  const me = await api.me();
  log.info("mimi_api", { base: settings.apiBase, mode: me.mode, role: me.role, own_session: !settings.token });
  const bridge = new Bridge({ settings, api, state, log, converter: ffmpegConverter(settings.ffmpeg) });
  const transport = await makeTransport();
  const stop = async (sig: string) => {
    log.info("stopping", { signal: sig });
    await transport.stop().catch(() => undefined);
    await bridge.idle();
    process.exit(0);
  };
  process.once("SIGINT", () => void stop("SIGINT"));
  process.once("SIGTERM", () => void stop("SIGTERM"));
  await bridge.run(transport);
}

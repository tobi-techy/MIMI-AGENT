import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const DEFAULT_API_BASE = "https://mimi-agent-tobi-omotade-2cd167ac.atlasflow.dev";

export interface SenderEntry {
  /** Normalised handle (phone digits or lower-case email). */
  key: string;
  shopId: string;
  /** The shop's phone in the Coral Lane demo book: what Mimi knows the shop by. */
  shopPhone: string;
  shopName: string;
  label: string;
}

export interface Limits {
  perSenderPerMinute: number;
  perSenderPerDay: number;
  /** Our own cap under Spectrum's 5,000 sends per server per day. */
  dailySendCap: number;
  /** New senders we take on per day (Spectrum: 50 new conversations per line per day). */
  newSendersPerDay: number;
}

export interface Settings {
  apiBase: string;
  /** A fixed bearer: a console's demo session token, or a channel key. Empty: the bridge opens its own demo session. */
  token: string;
  senders: Map<string, SenderEntry>;
  stateDir: string;
  pauseFile: string;
  pausedEnv: boolean;
  logBodies: boolean;
  pollApprovals: boolean;
  pollEveryMs: number;
  httpTimeoutMs: number;
  turnTimeoutMs: number;
  ffmpeg: string;
  limits: Limits;
}

/** Phones compare by digits only ("+1 (555) 010-2000" == "15550102000"); emails lower-case. */
export function normaliseHandle(raw: string): string {
  let h = raw.trim().replace(/^(tel:|mailto:|sms:|imessage:)/i, "");
  if (h.includes("@")) return h.toLowerCase();
  h = h.replace(/[^\d]/g, "");
  return h;
}

/** Last four of a handle, for logs. Never the whole thing. */
export function maskHandle(raw: string): string {
  const k = normaliseHandle(raw);
  if (k.includes("@")) return `***@${k.split("@")[1] ?? ""}`;
  return k.length <= 4 ? "****" : `***${k.slice(-4)}`;
}

interface ShopRow {
  shop_id: string;
  name: string;
  phone: string;
}

const HERE = dirname(fileURLToPath(import.meta.url));
export const SHOPS_FILE = resolve(HERE, "..", "data", "coral-lane-shops.json");

export function loadShops(path = SHOPS_FILE): Map<string, ShopRow> {
  const data = JSON.parse(readFileSync(path, "utf8")) as { shops: ShopRow[] };
  return new Map(data.shops.map((s) => [s.shop_id, s]));
}

interface RawConfig {
  senders?: { handle?: string; shop_id?: string; label?: string }[];
}

/** Read the allowlist: who may text Mimi, and which demo shop each of them plays. */
export function parseSenders(raw: RawConfig, shops: Map<string, ShopRow>): Map<string, SenderEntry> {
  const out = new Map<string, SenderEntry>();
  const usedShops = new Set<string>();
  for (const [i, s] of (raw.senders ?? []).entries()) {
    const handle = String(s.handle ?? "");
    const key = normaliseHandle(handle);
    if (!key || (!key.includes("@") && key.length < 7)) {
      throw new Error(`config senders[${i}]: handle must be a phone number or an email`);
    }
    const shop = shops.get(String(s.shop_id ?? ""));
    if (!shop) throw new Error(`config senders[${i}]: unknown shop_id "${s.shop_id}" (see data/coral-lane-shops.json)`);
    if (out.has(key)) throw new Error(`config senders[${i}]: the same handle is listed twice`);
    if (usedShops.has(shop.shop_id)) {
      throw new Error(`config senders[${i}]: ${shop.shop_id} is already played by another sender`);
    }
    usedShops.add(shop.shop_id);
    out.set(key, {
      key,
      shopId: shop.shop_id,
      shopPhone: shop.phone,
      shopName: shop.name,
      label: String(s.label ?? ""),
    });
  }
  if (out.size === 0) throw new Error("config: add at least one sender to the allowlist");
  return out;
}

function int(env: NodeJS.ProcessEnv, name: string, dflt: number): number {
  const v = env[name];
  if (v === undefined || v === "") return dflt;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) throw new Error(`${name} must be a number`);
  return n;
}

function flag(env: NodeJS.ProcessEnv, name: string, dflt: boolean): boolean {
  const v = (env[name] ?? "").trim().toLowerCase();
  if (v === "") return dflt;
  return ["1", "true", "yes", "on"].includes(v);
}

export function loadSettings(env: NodeJS.ProcessEnv = process.env): Settings {
  const configPath = resolve(env.BRIDGE_CONFIG || "bridge.config.json");
  const raw = JSON.parse(readFileSync(configPath, "utf8")) as RawConfig;
  const stateDir = resolve(env.BRIDGE_STATE_DIR || "state");
  const limits: Limits = {
    perSenderPerMinute: int(env, "BRIDGE_PER_SENDER_PER_MINUTE", 6),
    perSenderPerDay: int(env, "BRIDGE_PER_SENDER_PER_DAY", 60),
    dailySendCap: Math.min(int(env, "BRIDGE_DAILY_SEND_CAP", 4500), 5000),
    newSendersPerDay: Math.min(int(env, "BRIDGE_NEW_SENDERS_PER_DAY", 45), 50),
  };
  return {
    apiBase: (env.MIMI_API_BASE || DEFAULT_API_BASE).replace(/\/+$/, ""),
    token: env.MIMI_TOKEN ?? "",
    senders: parseSenders(raw, loadShops()),
    stateDir,
    pauseFile: resolve(env.BRIDGE_PAUSE_FILE || join(stateDir, "PAUSE")),
    pausedEnv: flag(env, "BRIDGE_PAUSED", false),
    logBodies: flag(env, "BRIDGE_LOG_BODIES", false),
    pollApprovals: flag(env, "BRIDGE_POLL_APPROVALS", true),
    pollEveryMs: int(env, "BRIDGE_POLL_SECONDS", 10) * 1000,
    httpTimeoutMs: int(env, "BRIDGE_HTTP_TIMEOUT_SECONDS", 30) * 1000,
    turnTimeoutMs: int(env, "BRIDGE_TURN_TIMEOUT_SECONDS", 120) * 1000,
    ffmpeg: env.FFMPEG_PATH || "ffmpeg",
    limits,
  };
}

// Durable bridge state in one small JSON file, written atomically (temp file
// then rename). Holds message ids only, never message text.
//
// - done:   inbound message ids already handled, so a restart never posts twice.
// - sent:   Mimi reply ids already sent, so a re-posted turn (the API answers
//           duplicate: true with the same replies) never sends twice.
// - day:    today's counters for the rate limits (Africa/Lagos day).
// - shops:  when each shop first texted through the bridge, so the approval
//           poller never sends a reply from before that.

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const KEEP_MS = 7 * 24 * 3600 * 1000;

export interface DayCounters {
  day: string;
  sends: number;
  perSender: Record<string, number>;
  newSenders: string[];
}

export interface StateData {
  done: Record<string, number>;
  sent: Record<string, number>;
  day: DayCounters;
  shops: Record<string, { firstAt: number; conversationId: string }>;
  session?: { token: string; tenantId: string; expiresAt: string };
  /** Demo cap refusals: no turns for this scope until the reset time. */
  blockedUntil?: number;
  /** The API's words for that refusal, sent once to each shop that texts while blocked. */
  blockedReason?: string;
}

export function lagosDay(now: Date): string {
  // Africa/Lagos is UTC+1 all year (no daylight saving).
  return new Date(now.getTime() + 3600_000).toISOString().slice(0, 10);
}

function empty(now: Date): StateData {
  return { done: {}, sent: {}, day: { day: lagosDay(now), sends: 0, perSender: {}, newSenders: [] }, shops: {} };
}

export class State {
  readonly path: string;
  data: StateData;
  private readonly clock: () => Date;

  constructor(path: string, clock: () => Date = () => new Date()) {
    this.path = path;
    this.clock = clock;
    this.data = empty(clock());
    if (path && existsSync(path)) {
      this.data = { ...empty(clock()), ...(JSON.parse(readFileSync(path, "utf8")) as Partial<StateData>) };
    }
    this.rollDay();
  }

  private rollDay(): DayCounters {
    const today = lagosDay(this.clock());
    if (this.data.day.day !== today) this.data.day = { day: today, sends: 0, perSender: {}, newSenders: [] };
    return this.data.day;
  }

  get today(): DayCounters {
    return this.rollDay();
  }

  isDone(id: string): boolean {
    return id in this.data.done;
  }

  markDone(id: string): void {
    this.data.done[id] = this.clock().getTime();
    this.save();
  }

  wasSent(replyId: string): boolean {
    return replyId in this.data.sent;
  }

  markSent(replyId: string): void {
    this.data.sent[replyId] = this.clock().getTime();
    this.rollDay().sends += 1;
    this.save();
  }

  save(): void {
    if (!this.path) return;
    const cutoff = this.clock().getTime() - KEEP_MS;
    for (const bag of [this.data.done, this.data.sent]) {
      for (const [k, t] of Object.entries(bag)) if (t < cutoff) delete bag[k];
    }
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.data), { mode: 0o600 });
    renameSync(tmp, this.path);
  }
}

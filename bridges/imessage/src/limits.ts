// Rate limits that sit in front of the API's own demo caps (₦400 per visitor,
// ₦2,000 for everyone, per Lagos day) and Spectrum Cloud's quotas on the free
// plan (5,000 sends per server per day; 50 new conversations per line per day).

import type { Limits } from "./config.ts";
import type { State } from "./state.ts";

export type Verdict = { ok: true } | { ok: false; reason: "per_minute" | "per_day" | "new_senders" };

export class RateLimiter {
  private readonly recent = new Map<string, number[]>();
  private readonly limits: Limits;
  private readonly state: State;
  private readonly clock: () => number;

  constructor(limits: Limits, state: State, clock: () => number = () => Date.now()) {
    this.limits = limits;
    this.state = state;
    this.clock = clock;
  }

  /** May this sender's message go to Mimi now? Counts it when it may. */
  admit(sender: string): Verdict {
    const now = this.clock();
    const day = this.state.today;
    const win = (this.recent.get(sender) ?? []).filter((t) => now - t < 60_000);
    if (win.length >= this.limits.perSenderPerMinute) return { ok: false, reason: "per_minute" };
    if ((day.perSender[sender] ?? 0) >= this.limits.perSenderPerDay) return { ok: false, reason: "per_day" };
    if (!day.newSenders.includes(sender)) {
      if (day.newSenders.length >= this.limits.newSendersPerDay) return { ok: false, reason: "new_senders" };
      day.newSenders.push(sender);
    }
    win.push(now);
    this.recent.set(sender, win);
    day.perSender[sender] = (day.perSender[sender] ?? 0) + 1;
    this.state.save();
    return { ok: true };
  }

  /** Room left under our daily send cap. */
  canSend(): boolean {
    return this.state.today.sends < this.limits.dailySendCap;
  }
}

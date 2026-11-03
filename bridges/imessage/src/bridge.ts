// The bridge: iMessage in, Mimi's /v1/turns as a demo shop, ready replies out.
//
// Rules, in order, for each inbound message:
//  1. our own echoes, group chats and senders not on the allowlist are ignored;
//  2. a message id already handled is ignored (restarts never post twice);
//  3. the kill switch (BRIDGE_PAUSED=1 or the pause file) drops it unanswered;
//  4. demo cap refusals and our rate limits hold it back;
//  5. text, photos and voice notes go to POST /v1/turns as the mapped shop,
//     exactly like a WhatsApp message from that shop's number;
//  6. only replies with status "ready" on the shop channel for that shop are
//     sent back. "pending_approval" drafts never leave; when the owner approves
//     one in the console, the poller finds it "ready" on the thread and sends it.

import { existsSync } from "node:fs";
import { type SenderEntry, type Settings, maskHandle, normaliseHandle } from "./config.ts";
import { RateLimiter } from "./limits.ts";
import type { Logger } from "./log.ts";
import { type Converter, UnsupportedMedia, apiKind, prepareForApi } from "./media.ts";
import { ApiRefused, DemoLimited, type MediaRef, type MimiClient, type Reply, type TurnBody } from "./mimi.ts";
import type { State } from "./state.ts";
import type { Conversation, InboundMessage, MediaPart, Transport } from "./types.ts";

export const SHOP_CHANNEL = "shop_whatsapp";

export const NOTES = {
  unsupported: "Sorry, I could not open that file. Please send a photo, a screenshot or a voice note.",
  failed: "Sorry, something went wrong on my side and I could not read your message. Please send it again in a few minutes.",
} as const;

export interface BridgeDeps {
  settings: Pick<Settings, "senders" | "pauseFile" | "pausedEnv" | "limits" | "pollApprovals" | "pollEveryMs">;
  api: MimiClient;
  state: State;
  log: Logger;
  converter: Converter;
  clock?: () => Date;
}

export class Bridge {
  private readonly d: BridgeDeps;
  private readonly limiter: RateLimiter;
  private readonly clock: () => Date;
  /** Per-sender chains: one sender's messages go in order; senders do not wait on each other. */
  private readonly chains = new Map<string, Promise<void>>();
  /** shop_id -> conversation to reply into (in memory; refilled by the shop's next message). */
  private readonly conversations = new Map<string, Conversation>();
  private readonly busyShops = new Set<string>();
  /** Reply ids being sent right now (claimed synchronously so two paths never both send). */
  private readonly claimed = new Set<string>();
  private readonly notified = new Set<string>();
  private timer: ReturnType<typeof setInterval> | undefined;

  constructor(d: BridgeDeps) {
    this.d = d;
    this.clock = d.clock ?? (() => new Date());
    this.limiter = new RateLimiter(d.settings.limits, d.state, () => this.clock().getTime());
  }

  paused(): boolean {
    return this.d.settings.pausedEnv || existsSync(this.d.settings.pauseFile);
  }

  /** Reads the transport until it ends. Each message is queued on its sender's chain. */
  async run(transport: Transport): Promise<void> {
    this.d.log.info("bridge_started", { transport: transport.name, senders: this.d.settings.senders.size, paused: this.paused() });
    if (this.d.settings.pollApprovals) {
      this.timer = setInterval(() => void this.pollApprovals(), this.d.settings.pollEveryMs);
    }
    try {
      for await (const msg of transport.messages()) this.enqueue(msg);
      await this.idle();
    } finally {
      if (this.timer) clearInterval(this.timer);
    }
  }

  enqueue(msg: InboundMessage): Promise<void> {
    const key = normaliseHandle(msg.sender) || "unknown";
    const next = (this.chains.get(key) ?? Promise.resolve())
      .then(async () => {
        await this.handle(msg);
      })
      .catch((e: unknown) => this.d.log.error("handle_crashed", { error: (e as Error).message }));
    this.chains.set(key, next);
    return next;
  }

  async idle(): Promise<void> {
    await Promise.all([...this.chains.values()]);
  }

  /** One inbound message, start to finish. Returns what happened (for logs and tests). */
  async handle(msg: InboundMessage): Promise<string> {
    const { log, state } = this.d;
    const who = maskHandle(msg.sender);
    if (msg.outbound) return "own_message";
    if (msg.isGroup) {
      log.info("ignored", { reason: "group_chat", platform: msg.platform });
      return "group_chat";
    }
    const entry = this.d.settings.senders.get(normaliseHandle(msg.sender));
    if (!entry) {
      log.info("ignored", { reason: "not_allowlisted", from: who });
      return "not_allowlisted";
    }
    const doneKey = `${msg.platform}:${msg.id}`;
    if (state.isDone(doneKey)) {
      log.info("ignored", { reason: "duplicate", from: who });
      return "duplicate";
    }
    if (this.paused()) {
      log.warn("paused_dropped", { from: who, shop: entry.shopId });
      return "paused";
    }
    const blocked = state.data.blockedUntil ?? 0;
    if (blocked > this.clock().getTime()) {
      log.warn("demo_capped_dropped", { from: who, until: new Date(blocked).toISOString() });
      // Each shop that texts while blocked hears why once, not only the one that hit the cap.
      if (state.data.blockedReason) {
        await this.sendNote(msg.conversation, `limit:${entry.shopId}:${blocked}`, state.data.blockedReason);
      }
      state.markDone(doneKey);
      return "demo_capped";
    }
    const v = this.limiter.admit(entry.key);
    if (!v.ok) {
      log.warn("rate_limited", { from: who, reason: v.reason });
      state.markDone(doneKey);
      return `rate_limited:${v.reason}`;
    }

    this.conversations.set(entry.shopId, msg.conversation);
    const known = state.data.shops[entry.shopId];
    state.data.shops[entry.shopId] = {
      firstAt: known?.firstAt ?? msg.at.getTime(),
      conversationId: msg.conversation.id,
    };
    state.save();

    this.busyShops.add(entry.shopId);
    try {
      return await this.relay(msg, entry, doneKey);
    } finally {
      this.busyShops.delete(entry.shopId);
    }
  }

  private async relay(msg: InboundMessage, entry: SenderEntry, doneKey: string): Promise<string> {
    const { log, state, api } = this.d;
    const text = msg.parts.flatMap((p) => (p.kind === "text" ? [p.text] : [])).join("\n").trim();
    const media = msg.parts.filter((p): p is MediaPart => p.kind !== "text");
    log.info("inbound", { from: maskHandle(msg.sender), shop: entry.shopId, text: text.length > 0, media: media.map((m) => m.kind) });
    log.body("inbound_text", text, { shop: entry.shopId });

    const turns: TurnBody[] = [];
    let unsupported = false;
    let failedMedia = false;
    const base = { source: "shop_message" as const, phone: entry.shopPhone, received_at: msg.at.toISOString() };
    if (media.length === 0) {
      if (text) turns.push({ ...base, message_id: `imsg:${msg.id}`, text });
    } else {
      for (const [i, part] of media.entries()) {
        let ref: MediaRef;
        try {
          const raw = await part.read();
          const ready = await prepareForApi(raw, part.kind, this.d.converter);
          ref = await api.upload(ready.bytes, ready.mime);
          ref.kind = apiKind(ready.mime);
        } catch (e) {
          if (e instanceof UnsupportedMedia || e instanceof ApiRefused) {
            log.warn("attachment_refused", { shop: entry.shopId, kind: part.kind, reason: e.message });
            unsupported = true;
            continue;
          }
          if (e instanceof DemoLimited) return this.capped(msg, entry, doneKey, e);
          // A download or upload that kept failing after retries.
          log.error("attachment_failed", { shop: entry.shopId, kind: part.kind, reason: (e as Error).message });
          failedMedia = true;
          continue;
        }
        turns.push({
          ...base,
          message_id: i === 0 ? `imsg:${msg.id}` : `imsg:${msg.id}:${i}`,
          text: turns.length === 0 ? text : "",
          media: ref,
        });
      }
      if (turns.length === 0 && text) turns.push({ ...base, message_id: `imsg:${msg.id}`, text });
    }

    if (unsupported) await this.sendNote(msg.conversation, `${doneKey}:unsupported`, NOTES.unsupported);
    if (failedMedia) await this.sendNote(msg.conversation, `${doneKey}:failed`, NOTES.failed);

    let outcome = turns.length ? "relayed" : "nothing_to_send";
    for (const body of turns) {
      try {
        const turn = await api.postTurn(body);
        log.info("turn", { shop: entry.shopId, state: turn.state, status: turn.status, duplicate: turn.duplicate, replies: turn.replies.length });
        if (turn.state === "failed" && Bridge.sendable(turn.replies, entry.shopId).length === 0) {
          // A failed turn writes its note to the owner console only; the phone still needs an answer.
          await this.sendNote(msg.conversation, `${doneKey}:failed`, NOTES.failed);
          outcome = "failed";
          break;
        }
        await this.sendReplies(msg.conversation, entry, turn.replies);
      } catch (e) {
        if (e instanceof DemoLimited) return this.capped(msg, entry, doneKey, e);
        log.error("turn_failed", { shop: entry.shopId, reason: (e as Error).message });
        await this.sendNote(msg.conversation, `${doneKey}:failed`, NOTES.failed);
        outcome = "failed";
        break;
      }
    }
    state.markDone(doneKey);
    return outcome;
  }

  /** Only "ready" replies on the shop channel for this shop. Drafts never leave. */
  static sendable(replies: Reply[], shopId: string): Reply[] {
    return replies.filter((r) => r.status === "ready" && r.channel === SHOP_CHANNEL && r.shop_id === shopId && r.text.trim());
  }

  private async sendReplies(conv: Conversation, entry: SenderEntry, replies: Reply[]): Promise<void> {
    const held = replies.filter((r) => r.status === "pending_approval").length;
    if (held) this.d.log.info("held_for_owner", { shop: entry.shopId, drafts: held });
    for (const r of Bridge.sendable(replies, entry.shopId)) {
      await this.sendReply(conv, entry, r.message_id, r.text, Boolean(r.voice));
    }
  }

  private async sendReply(conv: Conversation, entry: SenderEntry, id: string, text: string, voice: boolean): Promise<void> {
    const { state, log, api } = this.d;
    if (state.wasSent(id) || this.claimed.has(id)) return;
    if (!this.limiter.canSend()) {
      log.warn("daily_send_cap_reached", { shop: entry.shopId });
      return;
    }
    this.claimed.add(id);
    try {
      await conv.send({ kind: "text", text });
      state.markSent(id);
      log.info("sent", { shop: entry.shopId, voice });
      log.body("sent_text", text, { shop: entry.shopId });
      if (voice) {
        try {
          const audio = await api.voice(id);
          const m4a = await this.d.converter.convert(audio.bytes, "m4a");
          await conv.send({ kind: "voice", bytes: m4a, mime: "audio/mp4", name: "mimi.m4a" });
        } catch (e) {
          log.warn("voice_reply_skipped", { shop: entry.shopId, reason: (e as Error).message });
        }
      }
    } catch (e) {
      log.error("send_failed", { shop: entry.shopId, reason: (e as Error).message });
    } finally {
      this.claimed.delete(id);
    }
  }

  private async sendNote(conv: Conversation, key: string, text: string): Promise<void> {
    if (this.d.state.wasSent(key) || !this.limiter.canSend()) return;
    try {
      await conv.send({ kind: "text", text });
      this.d.state.markSent(key);
    } catch (e) {
      this.d.log.error("send_failed", { reason: (e as Error).message });
    }
  }

  private async capped(msg: InboundMessage, entry: SenderEntry, doneKey: string, e: DemoLimited): Promise<string> {
    const { state, log } = this.d;
    const resets = Date.parse(e.limit.resets_at);
    // Paused (no reset time): hold back for 10 minutes, then try again.
    state.data.blockedUntil = Number.isFinite(resets) ? resets : this.clock().getTime() + 10 * 60_000;
    state.data.blockedReason = e.limit.message || undefined;
    state.save();
    log.warn("demo_limit", { shop: entry.shopId, code: e.limit.code, until: new Date(state.data.blockedUntil).toISOString() });
    const noteKey = `limit:${entry.shopId}:${state.data.blockedUntil}`;
    if (!this.notified.has(noteKey) && e.limit.message && !state.wasSent(noteKey)) {
      this.notified.add(noteKey);
      await this.sendNote(msg.conversation, noteKey, e.limit.message);
    }
    state.markDone(doneKey);
    return `demo_limit:${e.limit.code}`;
  }

  /** Replies released later (the owner approved a draft): "ready" on the shop thread, not yet sent. */
  async pollApprovals(): Promise<number> {
    const { state, log, api } = this.d;
    if (this.paused() || !api.tenantId) return 0;
    let sent = 0;
    for (const [shopId, conv] of this.conversations) {
      if (this.busyShops.has(shopId)) continue;
      const entry = [...this.d.settings.senders.values()].find((s) => s.shopId === shopId);
      const since = state.data.shops[shopId]?.firstAt ?? Number.POSITIVE_INFINITY;
      if (!entry) continue;
      try {
        const messages = await api.thread(`${api.tenantId}:shop:${shopId}`);
        for (const m of messages) {
          if (m.direction !== "out" || m.status !== "ready" || m.channel !== SHOP_CHANNEL || m.shop_id !== shopId) continue;
          if (!(Date.parse(m.at) >= since - 1000) || state.wasSent(m.message_id) || !m.text.trim()) continue;
          await this.sendReply(conv, entry, m.message_id, m.text, Boolean(m.voice));
          sent += 1;
        }
      } catch (e) {
        log.warn("poll_failed", { shop: shopId, reason: (e as Error).message });
      }
    }
    return sent;
  }
}

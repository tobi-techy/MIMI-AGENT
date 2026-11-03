import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { describe, it } from "node:test";
import { Bridge, NOTES } from "../src/bridge.ts";
import {
  ALICE,
  BOB,
  CAF,
  FakeApi,
  FakeConversation,
  HEIC,
  PNG,
  STRANGER,
  inbound,
  json,
  reply,
  rig,
  turn,
} from "./fakes.ts";

describe("who gets through", () => {
  it("ignores senders not on the allowlist, group chats and our own echoes, without calling the API", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([])));
    assert.equal(await r.bridge.handle(inbound({ sender: STRANGER })), "not_allowlisted");
    assert.equal(await r.bridge.handle(inbound({ isGroup: true })), "group_chat");
    assert.equal(await r.bridge.handle(inbound({ outbound: true })), "own_message");
    assert.equal(r.api.calls.length, 0);
  });

  it("matches a handle however the phone number is written", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([])));
    assert.equal(await r.bridge.handle(inbound({ sender: "tel:+15550102000" })), "relayed");
    assert.equal(await r.bridge.handle(inbound({ sender: "BOB@Example.com", text: "hi" })), "relayed");
  });
});

describe("a text message", () => {
  it("posts one shop turn with the mapped shop's demo number, exactly like WhatsApp", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply()])));
    const msg = inbound({ text: "I don collect the goods" });
    assert.equal(await r.bridge.handle(msg), "relayed");
    const [call] = r.api.turns();
    assert.ok(call);
    assert.equal(call.path, "/v1/turns?wait=true");
    assert.equal(call.auth, "Bearer test-bearer");
    assert.deepEqual(call.json, {
      source: "shop_message",
      phone: "+2340000000100",
      received_at: "2026-11-03T12:00:00.000Z",
      message_id: `imsg:${msg.id}`,
      text: "I don collect the goods",
    });
    assert.deepEqual(msg.conversation.texts, ["Thank you. Your delivery is confirmed."]);
  });

  it("maps a second sender to their own shop", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply({ shop_id: "shop-002" })])));
    const msg = inbound({ sender: BOB });
    await r.bridge.handle(msg);
    assert.equal((r.api.turns()[0]?.json as { phone: string }).phone, "+2340000000101");
    assert.equal(msg.conversation.texts.length, 1);
  });
});

describe("only ready replies leave", () => {
  it("never sends pending_approval drafts, owner console lines or another shop's reply", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () =>
      json(
        200,
        turn([
          reply({ text: "ready one" }),
          reply({ text: "draft", status: "pending_approval" }),
          reply({ text: "owner note", channel: "owner_console", shop_id: "" }),
          reply({ text: "other shop", shop_id: "shop-009" }),
          reply({ text: "rejected", status: "rejected" }),
        ]),
      ),
    );
    const msg = inbound();
    await r.bridge.handle(msg);
    assert.deepEqual(msg.conversation.texts, ["ready one"]);
    assert.ok(r.log.lines.some((l) => l.event === "held_for_owner" && l.drafts === 1));
  });

  it("filters with the static helper too", () => {
    const out = Bridge.sendable(
      [
        { message_id: "a", channel: "shop_whatsapp", shop_id: "shop-001", text: "x", kind: "reply", status: "ready" },
        { message_id: "b", channel: "shop_whatsapp", shop_id: "shop-001", text: "y", kind: "reply", status: "pending_approval" },
      ],
      "shop-001",
    );
    assert.deepEqual(
      out.map((o) => o.message_id),
      ["a"],
    );
  });
});

describe("idempotency", () => {
  it("handles the same iMessage guid once", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply()])));
    const msg = inbound();
    await r.bridge.handle(msg);
    assert.equal(await r.bridge.handle(msg), "duplicate");
    assert.equal(r.api.turns().length, 1);
    assert.equal(msg.conversation.texts.length, 1);
  });

  it("survives a restart: a new bridge on the same state file skips handled messages", async () => {
    const first = rig();
    first.api.on("POST", "/v1/turns", () => json(200, turn([reply()])));
    const msg = inbound();
    await first.bridge.handle(msg);
    const second = rig({ dir: first.dir });
    second.api.on("POST", "/v1/turns", () => json(200, turn([reply()])));
    assert.equal(await second.bridge.handle(msg), "duplicate");
    assert.equal(second.api.calls.length, 0);
  });

  it("a crash after sending but before marking done never sends the reply twice", async () => {
    const first = rig();
    const r1 = reply({ message_id: "out:fixed-1" });
    first.api.on("POST", "/v1/turns", () => json(200, turn([r1])));
    const msg = inbound();
    await first.bridge.handle(msg);
    // Simulate the crash: forget the inbound was done, keep what was sent.
    delete first.state.data.done[`imessage:${msg.id}`];
    first.state.save();
    const second = rig({ dir: first.dir });
    second.api.on("POST", "/v1/turns", () => json(200, turn([r1], { duplicate: true })));
    const again = inbound({ id: msg.id });
    await second.bridge.handle(again);
    assert.equal(second.api.turns().length, 1, "re-posted (the API dedupes by message_id)");
    assert.deepEqual(again.conversation.texts, [], "but nothing sent again");
  });
});

describe("kill switch", () => {
  it("BRIDGE_PAUSED drops messages unanswered", async () => {
    const r = rig({ pausedEnv: true });
    const msg = inbound();
    assert.equal(await r.bridge.handle(msg), "paused");
    assert.equal(r.api.calls.length, 0);
    assert.equal(msg.conversation.sent.length, 0);
  });

  it("the pause file works instantly and removing it resumes", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([])));
    writeFileSync(r.settings.pauseFile, "");
    assert.equal(await r.bridge.handle(inbound()), "paused");
    const { rmSync } = await import("node:fs");
    rmSync(r.settings.pauseFile);
    assert.equal(await r.bridge.handle(inbound()), "relayed");
  });
});

describe("attachments", () => {
  it("uploads a photo through /v1/media and sends it as the turn's media with the caption text", async () => {
    const r = rig();
    r.api.on("POST", "/v1/media", (c) =>
      json(201, { ref: "local:demo-abc/1", sha256: "ab".repeat(32), mime: c.contentType, kind: "image", size: 12 }),
    );
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply({ text: "I see the transfer." })])));
    const msg = inbound({
      parts: [
        { kind: "text", text: "I don pay" },
        { kind: "image", mime: "image/png", name: "IMG_1.png", read: async () => PNG },
      ],
    });
    assert.equal(await r.bridge.handle(msg), "relayed");
    const media = r.api.calls.find((c) => c.path === "/v1/media");
    assert.equal(media?.contentType, "image/png");
    assert.deepEqual(media?.bytes, PNG);
    const body = r.api.turns()[0]?.json as { text: string; media: Record<string, string> };
    assert.equal(body.text, "I don pay");
    assert.deepEqual(body.media, { kind: "image", ref: "local:demo-abc/1", mime: "image/png", sha256: "ab".repeat(32) });
    assert.equal(r.conv.calls.length, 0);
    assert.deepEqual(msg.conversation.texts, ["I see the transfer."]);
  });

  it("converts an iMessage voice note (.caf) to Ogg before upload", async () => {
    const r = rig();
    r.api.on("POST", "/v1/media", (c) => json(201, { ref: "local:v", sha256: "cd".repeat(32), mime: c.contentType, kind: "voice", size: 8 }));
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply()])));
    const msg = inbound({ parts: [{ kind: "voice", mime: "audio/x-caf", name: "Audio Message.caf", read: async () => CAF }] });
    await r.bridge.handle(msg);
    assert.deepEqual(r.conv.calls, ["ogg"]);
    assert.equal(r.api.calls.find((c) => c.path === "/v1/media")?.contentType, "audio/ogg");
    assert.equal((r.api.turns()[0]?.json as { media: { kind: string } }).media.kind, "voice");
  });

  it("converts a HEIC photo to JPEG", async () => {
    const r = rig();
    r.api.on("POST", "/v1/media", (c) => json(201, { ref: "local:p", sha256: "ef".repeat(32), mime: c.contentType, kind: "image", size: 6 }));
    r.api.on("POST", "/v1/turns", () => json(200, turn([])));
    await r.bridge.handle(inbound({ parts: [{ kind: "image", mime: "image/heic", name: "IMG.HEIC", read: async () => HEIC }] }));
    assert.deepEqual(r.conv.calls, ["jpg"]);
    assert.equal(r.api.calls.find((c) => c.path === "/v1/media")?.contentType, "image/jpeg");
  });

  it("a file that cannot be converted gets one plain note and no turn", async () => {
    const r = rig();
    r.conv.fail = true;
    const msg = inbound({ parts: [{ kind: "voice", mime: "audio/x-caf", name: "a.caf", read: async () => CAF }] });
    assert.equal(await r.bridge.handle(msg), "nothing_to_send");
    assert.equal(r.api.calls.length, 0);
    assert.deepEqual(msg.conversation.texts, [NOTES.unsupported]);
  });

  it("several photos become one turn each; the text rides on the first", async () => {
    const r = rig();
    let n = 0;
    r.api.on("POST", "/v1/media", (c) => json(201, { ref: `local:${++n}`, sha256: "aa".repeat(32), mime: c.contentType, kind: "image", size: 1 }));
    r.api.on("POST", "/v1/turns", () => json(200, turn([])));
    const msg = inbound({
      parts: [
        { kind: "text", text: "my shelf" },
        { kind: "image", mime: "image/png", name: "1.png", read: async () => PNG },
        { kind: "image", mime: "image/png", name: "2.png", read: async () => PNG },
      ],
    });
    await r.bridge.handle(msg);
    const bodies = r.api.turns().map((c) => c.json as { message_id: string; text: string });
    assert.deepEqual(
      bodies.map((b) => [b.message_id, b.text]),
      [
        [`imsg:${msg.id}`, "my shelf"],
        [`imsg:${msg.id}:1`, ""],
      ],
    );
  });
});

describe("voice replies", () => {
  it("sends the text, then the reply's voice note converted to m4a", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply({ message_id: "out:v1", voice: { script: "x" } })])));
    r.api.on("GET", "/v1/messages/out%3Av1/voice", () => new Response(new Uint8Array([0x4f, 0x67, 0x67, 0x53]), { headers: { "content-type": "audio/ogg" } }));
    const msg = inbound();
    await r.bridge.handle(msg);
    assert.deepEqual(
      msg.conversation.sent.map((s) => s.kind),
      ["text", "voice"],
    );
    assert.deepEqual(r.conv.calls, ["m4a"]);
  });
});

describe("errors, retries and timeouts", () => {
  it("retries a 5xx with backoff and then succeeds", async () => {
    const r = rig();
    let n = 0;
    r.api.on("POST", "/v1/turns", () => (++n < 3 ? json(502, {}) : json(200, turn([reply()]))));
    const msg = inbound();
    assert.equal(await r.bridge.handle(msg), "relayed");
    assert.equal(r.api.turns().length, 3);
    assert.equal(msg.conversation.texts.length, 1);
  });

  it("retries a network error", async () => {
    const r = rig();
    let n = 0;
    r.api.on("POST", "/v1/turns", () => {
      if (++n === 1) throw new TypeError("fetch failed");
      return json(200, turn([]));
    });
    assert.equal(await r.bridge.handle(inbound()), "relayed");
  });

  it("times out a hung request, retries, and tells the shop when every try failed", async () => {
    const api = new FakeApi();
    const plain = api.fetch;
    api.fetch = (url, init) =>
      url.includes("/v1/turns")
        ? new Promise((_, fail) => init?.signal?.addEventListener("abort", () => fail(new DOMException("aborted", "AbortError"))))
        : plain(url, init);
    const r = rig({ api, timeoutMs: 20, turnTimeoutMs: 20 });
    const msg = inbound();
    assert.equal(await r.bridge.handle(msg), "failed");
    assert.equal(r.log.lines.filter((l) => l.event === "api_retry" && l.reason === "AbortError").length, 4);
    assert.deepEqual(msg.conversation.texts, [NOTES.failed]);
  });

  it("does not retry a 4xx and sends one plain note", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(422, { detail: "bad" }));
    const msg = inbound();
    assert.equal(await r.bridge.handle(msg), "failed");
    assert.equal(r.api.turns().length, 1);
    assert.deepEqual(msg.conversation.texts, [NOTES.failed]);
  });

  it("polls GET /v1/turns/{id} while the API answers 202", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(202, turn([], { state: "leased" })));
    let polls = 0;
    r.api.on("GET", "/v1/turns/turn_1", () => (++polls < 2 ? json(200, turn([], { state: "leased" })) : json(200, turn([reply()]))));
    const msg = inbound();
    await r.bridge.handle(msg);
    assert.equal(polls, 2);
    assert.equal(msg.conversation.texts.length, 1);
  });

  it("a failed iMessage send is logged and the bridge keeps going", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply()])));
    const conversation = new FakeConversation();
    conversation.failNext = true;
    assert.equal(await r.bridge.handle(inbound({ conversation })), "relayed");
    assert.ok(r.log.lines.some((l) => l.event === "send_failed"));
  });
});

describe("demo caps and rate limits", () => {
  it("a demo cap refusal holds every later message until the reset and tells the shop once", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () =>
      json(429, {
        detail: "cap",
        demo_limit: {
          code: "visitor_daily_cap",
          message: "You have used today's demo allowance; it resets at midnight Lagos time.",
          resets_at: "2999-01-01T00:00:00+01:00",
        },
      }),
    );
    const conversation = new FakeConversation();
    assert.equal(await r.bridge.handle(inbound({ conversation })), "demo_limit:visitor_daily_cap");
    assert.equal(await r.bridge.handle(inbound({ conversation })), "demo_capped");
    assert.equal(r.api.turns().length, 1, "never retried");
    assert.deepEqual(conversation.texts, ["You have used today's demo allowance; it resets at midnight Lagos time."]);
  });

  it("limits each sender per minute", async () => {
    const r = rig({ limits: { perSenderPerMinute: 2 } });
    r.api.on("POST", "/v1/turns", () => json(200, turn([])));
    assert.equal(await r.bridge.handle(inbound()), "relayed");
    assert.equal(await r.bridge.handle(inbound()), "relayed");
    assert.equal(await r.bridge.handle(inbound()), "rate_limited:per_minute");
    assert.equal(await r.bridge.handle(inbound({ sender: BOB })), "relayed", "another sender is not held back");
  });

  it("caps new senders per day (Spectrum: 50 new conversations per line per day)", async () => {
    const r = rig({ limits: { newSendersPerDay: 1 } });
    r.api.on("POST", "/v1/turns", () => json(200, turn([])));
    assert.equal(await r.bridge.handle(inbound()), "relayed");
    assert.equal(await r.bridge.handle(inbound({ sender: BOB })), "rate_limited:new_senders");
  });

  it("stops sending at the daily send cap (Spectrum: 5,000 per server per day)", async () => {
    const r = rig({ limits: { dailySendCap: 1 } });
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply({ text: "one" }), reply({ text: "two" })])));
    const msg = inbound();
    await r.bridge.handle(msg);
    assert.deepEqual(msg.conversation.texts, ["one"]);
    assert.ok(r.log.lines.some((l) => l.event === "daily_send_cap_reached"));
  });
});

describe("replies the owner approves later", () => {
  it("the poller sends a ready shop reply from after the shop's first message, once", async () => {
    const r = rig();
    r.state.data.session = { token: "", tenantId: "demo-abc", expiresAt: "" };
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply({ text: "draft", status: "pending_approval", message_id: "out:d1" })])));
    const conversation = new FakeConversation();
    await r.bridge.handle(inbound({ conversation }));
    assert.deepEqual(conversation.texts, []);
    r.api.on("GET", "/v1/threads/demo-abc%3Ashop%3Ashop-001", () =>
      json(200, {
        messages: [
          { ...reply({ message_id: "out:old", text: "seeded history", at: "2026-10-20T09:00:00+00:00" }), direction: "out" },
          { ...reply({ message_id: "out:d1", text: "Approved: your order is on the way." }), direction: "out" },
          { ...reply({ message_id: "out:d2", text: "still waiting", status: "pending_approval" }), direction: "out" },
          { ...reply({ message_id: "in:x", text: "hello" }), direction: "in" },
        ],
      }),
    );
    assert.equal(await r.bridge.pollApprovals(), 1);
    assert.equal(await r.bridge.pollApprovals(), 0);
    assert.deepEqual(conversation.texts, ["Approved: your order is on the way."]);
  });

  it("the poller does nothing while paused", async () => {
    const r = rig();
    r.state.data.session = { token: "", tenantId: "demo-abc", expiresAt: "" };
    writeFileSync(r.settings.pauseFile, "");
    assert.equal(await r.bridge.pollApprovals(), 0);
    assert.equal(r.api.calls.length, 0);
  });
});

describe("logs", () => {
  it("keep message bodies and full handles out by default", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply({ text: "secret reply words" })])));
    await r.bridge.handle(inbound({ text: "secret shop words" }));
    const all = JSON.stringify(r.log.lines);
    assert.ok(!all.includes("secret"));
    assert.ok(!all.includes("5550102000"));
    assert.ok(all.includes("***2000"));
  });
});

describe("the run loop", () => {
  it("drains a transport, one chain per sender", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([reply()])));
    const msgs = [inbound(), inbound({ sender: BOB }), inbound({ sender: STRANGER }), inbound({ sender: ALICE })];
    await r.bridge.run({
      name: "fake",
      async *messages() {
        yield* msgs;
      },
      stop: async () => undefined,
    });
    assert.equal(r.api.turns().length, 3);
  });
});

describe("attachment downloads", () => {
  it("a photo that cannot be downloaded gets the failure note and the text still goes", async () => {
    const r = rig();
    r.api.on("POST", "/v1/turns", () => json(200, turn([])));
    const msg = inbound({
      parts: [
        { kind: "text", text: "see photo" },
        { kind: "image", mime: "image/png", name: "1.png", read: async () => Promise.reject(new Error("download failed")) },
      ],
    });
    assert.equal(await r.bridge.handle(msg), "relayed");
    assert.equal((r.api.turns()[0]?.json as { text: string }).text, "see photo");
    assert.deepEqual(msg.conversation.texts, [NOTES.failed]);
  });
});

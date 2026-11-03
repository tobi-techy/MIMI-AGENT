import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { SHOPS_FILE, loadShops, maskHandle, normaliseHandle, parseSenders } from "../src/config.ts";
import { memoryLogger } from "../src/log.ts";
import { prepareForApi, sniff } from "../src/media.ts";
import { MimiClient } from "../src/mimi.ts";
import { type SpectrumApp, guidLooksGroup, partsOf, spectrumTransport, toOutbound } from "../src/spectrum.ts";
import { State, lagosDay } from "../src/state.ts";
import { CAF, FakeApi, FakeConverter, HEIC, JPG, M4A, OGG, PNG, json } from "./fakes.ts";

describe("config", () => {
  it("normalises and masks handles", () => {
    assert.equal(normaliseHandle("+1 (555) 010-2999"), "15550102999");
    assert.equal(normaliseHandle("tel:+15550102999"), "15550102999");
    assert.equal(normaliseHandle(" Tobi@Example.COM "), "tobi@example.com");
    assert.equal(maskHandle("+1 (555) 010-2999"), "***2999");
    assert.equal(maskHandle("tobi@example.com"), "***@example.com");
  });

  it("refuses a bad allowlist with a plain reason", () => {
    const shops = loadShops();
    assert.throws(() => parseSenders({ senders: [] }, shops), /at least one sender/);
    assert.throws(() => parseSenders({ senders: [{ handle: "+15550100000", shop_id: "shop-999" }] }, shops), /unknown shop_id/);
    assert.throws(() => parseSenders({ senders: [{ handle: "12", shop_id: "shop-001" }] }, shops), /phone number or an email/);
    assert.throws(
      () => parseSenders({ senders: [{ handle: "+15550100000", shop_id: "shop-001" }, { handle: "15550100000", shop_id: "shop-002" }] }, shops),
      /listed twice/,
    );
    assert.throws(
      () => parseSenders({ senders: [{ handle: "+15550100000", shop_id: "shop-001" }, { handle: "+15550100001", shop_id: "shop-001" }] }, shops),
      /already played/,
    );
  });

  it("the bundled shop list matches the Coral Lane fixture in this repo", () => {
    const fixture = resolve(dirname(fileURLToPath(import.meta.url)), "../../../mimi/world/fixtures/coral_lane_seed2610.json");
    if (!existsSync(fixture)) return;
    const world = JSON.parse(readFileSync(fixture, "utf8")) as { shops: { shop_id: string; name: string; phone: string }[] };
    const ours = JSON.parse(readFileSync(SHOPS_FILE, "utf8")) as { shops: unknown[] };
    assert.deepEqual(ours.shops, world.shops.map((s) => ({ shop_id: s.shop_id, name: s.name, phone: s.phone })));
  });

  it("the example config parses", () => {
    const ex = JSON.parse(readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "../bridge.config.example.json"), "utf8"));
    assert.ok(parseSenders(ex, loadShops()).size >= 1);
  });
});

describe("media", () => {
  it("sniffs files by their first bytes", () => {
    assert.equal(sniff(PNG), "image/png");
    assert.equal(sniff(JPG), "image/jpeg");
    assert.equal(sniff(OGG), "audio/ogg");
    assert.equal(sniff(M4A), "audio/mp4");
    assert.equal(sniff(CAF), "audio/x-caf");
    assert.equal(sniff(HEIC), "image/heic");
  });

  it("passes accepted types straight through and converts the rest", async () => {
    const conv = new FakeConverter();
    assert.deepEqual(await prepareForApi(M4A, "voice", conv), { bytes: M4A, mime: "audio/mp4" });
    assert.equal((await prepareForApi(CAF, "voice", conv)).mime, "audio/ogg");
    assert.deepEqual(conv.calls, ["ogg"]);
  });

  it("refuses a file over 10 MB", async () => {
    const big = new Uint8Array(10 * 1024 * 1024 + 1);
    big.set(PNG);
    await assert.rejects(prepareForApi(big, "image", new FakeConverter()), /10 MB/);
  });
});

describe("state", () => {
  it("counts days on Lagos time", () => {
    assert.equal(lagosDay(new Date("2026-11-03T23:30:00Z")), "2026-11-04");
    assert.equal(lagosDay(new Date("2026-11-03T22:59:00Z")), "2026-11-03");
  });

  it("resets the day's counters at Lagos midnight", () => {
    let now = new Date("2026-11-03T12:00:00Z");
    const s = new State("", () => now);
    s.markSent("a");
    assert.equal(s.today.sends, 1);
    now = new Date("2026-11-04T00:00:00Z");
    assert.equal(s.today.sends, 0);
  });
});

describe("demo session", () => {
  it("opens its own demo session when no token is given, and renews it once on 401", async () => {
    const api = new FakeApi();
    let sessions = 0;
    api.on("POST", "/v1/demo/sessions", () =>
      json(201, { token: `tok-${++sessions}`, tenant_id: `demo-${sessions}`, expires_at: "2999-01-01T00:00:00Z" }),
    );
    let first = true;
    api.on("GET", "/v1/me", (c) => {
      if (first) {
        first = false;
        return json(401, { detail: "demo session expired" });
      }
      return json(200, { tenant_id: c.auth === "Bearer tok-2" ? "demo-2" : "?", role: "owner", mode: "demo" });
    });
    const state = new State("");
    const client = new MimiClient({ base: api.base, token: "", fetch: api.fetch, log: memoryLogger(), state, timeoutMs: 100, turnTimeoutMs: 100, sleep: async () => undefined });
    const me = await client.me();
    assert.equal(me.tenant_id, "demo-2");
    assert.equal(sessions, 2);
    assert.equal(client.tenantId, "demo-2");
  });

  it("with a fixed token, a 401 is not papered over", async () => {
    const api = new FakeApi();
    api.on("GET", "/v1/me", () => json(401, { detail: "demo session expired" }));
    const client = new MimiClient({ base: api.base, token: "fixed", fetch: api.fetch, log: memoryLogger(), state: new State(""), timeoutMs: 100, turnTimeoutMs: 100 });
    await assert.rejects(client.me(), /401/);
    assert.equal(api.calls.length, 1);
  });
});

describe("spectrum adapter", () => {
  it("reads text, attachments, voice notes and grouped items; skips the rest", async () => {
    assert.deepEqual(partsOf({ type: "text", text: "hi" }), [{ kind: "text", text: "hi" }]);
    assert.deepEqual(partsOf({ type: "reaction" }), []);
    const voice = partsOf({ type: "voice", mimeType: "audio/x-caf", name: "a.caf", read: async () => CAF });
    assert.equal(voice[0]?.kind, "voice");
    const photo = partsOf({ type: "attachment", mimeType: "image/jpeg", name: "p.jpg", read: async () => JPG });
    assert.equal(photo[0]?.kind, "image");
    const audioAttachment = partsOf({ type: "attachment", mimeType: "audio/x-caf", read: async () => CAF });
    assert.equal(audioAttachment[0]?.kind, "voice");
    const group = partsOf({ type: "group", items: [{ content: { type: "text", text: "a" } }, { content: { type: "attachment", mimeType: "image/png", read: async () => PNG } }] });
    assert.deepEqual(group.map((g) => g.kind), ["text", "image"]);
  });

  it("spots group chat guids", () => {
    assert.equal(guidLooksGroup("any;+;chat123"), true);
    assert.equal(guidLooksGroup("any;-;+15550102000"), false);
  });

  it("wraps a Spectrum app as a transport and replies through space.send", async () => {
    const sent: unknown[] = [];
    const space = { id: "any;-;+15550102000", send: async (c: unknown) => void sent.push(c) };
    const app: SpectrumApp = {
      messages: (async function* () {
        yield [space as never, { id: "g1", platform: "imessage", direction: "inbound", timestamp: new Date(0), sender: { id: "x" }, content: { type: "text", text: "hey" } }];
      })(),
      stop: async () => undefined,
    };
    const t = spectrumTransport("test", app, { senderOf: () => "+15550102000", isGroup: () => false });
    for await (const m of t.messages()) {
      assert.equal(m.sender, "+15550102000");
      assert.equal(m.isGroup, false);
      assert.deepEqual(m.parts, [{ kind: "text", text: "hey" }]);
      await m.conversation.send({ kind: "text", text: "back" });
    }
    assert.deepEqual(sent, ["back"]);
    assert.equal(toOutbound({ kind: "text", text: "t" }), "t");
    assert.ok(toOutbound({ kind: "image", bytes: PNG, mime: "image/png", name: "a.png" }));
  });
});

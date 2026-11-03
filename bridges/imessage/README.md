# Mimi on iMessage (demo bridge)

This small service lets a real phone talk to Mimi over iMessage. Someone
texts Mimi's iMessage number. The bridge passes the message to the Mimi API
as one of the demo shops. When Mimi's reply is ready, the bridge texts it
back.

It uses Photon's hosted iMessage, called Spectrum Cloud
([docs](https://photon.codes/docs/spectrum-ts/providers/imessage)). No Mac
is needed: it runs anywhere Node runs.

## For judges

* For this demo, iMessage is how shops reach Mimi. In production the channel
  is WhatsApp. We are waiting for Meta to give us a WhatsApp number, so the
  Cloud API test number cannot be used yet.
* Both channels use the same API. A message goes to `POST /v1/turns` as
  `source: "shop_message"` from the shop's number, exactly like a WhatsApp
  message from that shop. Mimi does not know which app the message came from.
* Mimi never moves money. Replies that need the owner's approval stay in the
  console until the owner approves them. Only then does the bridge send them.
* Every shop is a made-up shop from the fictional Coral Lane Trading demo
  book.

## What it does

1. It listens for iMessages on the project's line.
2. It ignores group chats, its own messages, and anyone who is not on the
   allowlist in `bridge.config.json`.
3. It plays each allowlisted phone as one demo shop. Mimi sees that shop's
   own number from the demo book (`data/coral-lane-shops.json`).
4. It sends text, photos (like transfer screenshots or shelf photos) and voice
   notes to `POST /v1/turns`. Photos and voice notes are uploaded first
   through `POST /v1/media`. iMessage voice notes (`.caf`) are converted to Ogg
   and HEIC photos to JPEG with ffmpeg.
5. It texts back only replies whose status is `ready` on the shop channel.
   It never sends `pending_approval` drafts. It checks the shop's thread every
   10 seconds, so a draft the owner approves later in the console still
   reaches the phone. If a reply has a voice note, the bridge sends the text
   first and then the audio as m4a.
6. It handles each iMessage once, keyed by its message id, even after a
   restart. It also keeps track of the replies it has sent so none goes out
   twice.

## Set up the Photon project (done once)

The project already exists: **Mimi demo**, Free plan, iMessage cloud turned on.

* Project id: `48e46ed2-943d-4b9c-ae1c-cb8396a659d6` (not secret)
* iMessage line: the shared-pool number Photon shows on the project dashboard
  (kept out of this public repo)

To make a new project:

1. Sign in at [app.photon.codes](https://app.photon.codes).
2. Create a project and turn on iMessage (cloud).
3. Open the project's **Settings**. Copy the **project id** and the
   **project secret**.
4. Note the iMessage number the dashboard shows for the project.

Keep the secret private. It goes in `.env` and nowhere else. It is never
committed.

## Run it

You need Node 22.18 or newer (`node -v`) and ffmpeg.

```bash
# macOS: brew install node ffmpeg      Ubuntu: sudo apt install ffmpeg
cd bridges/imessage
npm ci
cp .env.example .env                   # then paste SPECTRUM_PROJECT_SECRET
cp bridge.config.example.json bridge.config.json
```

Edit `bridge.config.json` to list who may text Mimi and which shop each person
plays:

```json
{
  "senders": [
    { "handle": "+2348012345678", "shop_id": "shop-001", "label": "Tobi's iPhone plays Mama Sola Provisions" },
    { "handle": "friend@icloud.com", "shop_id": "shop-035", "label": "Teammate plays Mama Chidi Kiosk" }
  ]
}
```

* `handle` is the phone number or Apple ID email that will text. You can
  write the number with or without spaces.
* `shop_id` is a shop from `data/coral-lane-shops.json`. Each shop can be
  played by only one person.

Check the Mimi side first. This sends one fake message as the first shop and
prints Mimi's reply. iMessage is not involved and it costs a few naira:

```bash
npm run smoke -- "Good morning. How much do I owe you now?"
```

Then start the bridge:

```bash
npm start
```

From an allowlisted phone, text the project's iMessage line (from the Photon
dashboard). Mimi answers within a
few seconds.

With Bun installed, `bun --env-file=.env src/main-cloud.ts` also works.

### Docker (any small server)

```bash
docker build -t mimi-imessage-bridge bridges/imessage
docker run -d --restart unless-stopped --env-file bridges/imessage/.env \
  -v "$PWD/bridges/imessage/bridge.config.json:/app/bridge.config.json:ro" \
  -v mimi-bridge-state:/app/state mimi-imessage-bridge
```

It opens no port. It only connects out to Photon and the Mimi API. Run one
copy only.

### Keep a Mac awake while it runs

```bash
caffeinate -i npm start
```

## Show the iMessage shop in your console

If you set nothing, the bridge opens its own demo session. That is its own
copy of Coral Lane, and it renews the session when it expires. To make the
phone's messages show up in the console you are demoing, set `MIMI_TOKEN` in
`.env` to that console's demo session token and restart. When that session
expires, get a fresh token and restart.

## Kill switch

Any one of these pauses the bridge at once. While paused, new messages are
dropped without a reply and nothing goes to Mimi:

```bash
touch state/PAUSE        # pause now
rm state/PAUSE           # resume
```

You can also set `BRIDGE_PAUSED=1` in `.env` and restart, or just stop the
process (Ctrl+C).

## Limits

* **Demo caps (enforced by the API):** ₦400 per visitor and ₦2,000 for
  everyone, per Lagos day. When the API refuses, the bridge texts the API's
  one-line reason once to each shop that writes while it is blocked. Then it
  stays quiet until the cap resets.
* **Its own demo session lasts about an hour.** Without `MIMI_TOKEN`, each
  renewal is a fresh copy of Coral Lane (history starts over) and counts
  against the API's 20 demo sessions per network per day. For a run longer
  than an hour, set `MIMI_TOKEN` to a session you control and restart when it
  expires.
* **Approved drafts after a restart:** the bridge remembers which chat to
  answer in memory only. A draft the owner approves after a restart goes out
  when that shop next texts.
* **Spectrum Free plan:** shared-pool line, DMs only (no groups), 5,000
  messages per day and 50 new conversations per line per day. The bridge
  stops below that: 4,500 sends a day and 45 new senders a day. It only
  replies in chats a person started, so it never opens new conversations
  itself.
* **Per person:** at most 6 messages a minute and 60 a day. Change these in
  `.env`.

## Privacy and logs

Logs are one JSON line per event. They never include message text unless
`BRIDGE_LOG_BODIES=1`. Phone numbers show only their last 4 digits.
`state/bridge-state.json` keeps only message ids, daily counts and the
bridge's own demo session token, readable by the owner only. Do not commit
`state/`, `.env` or `bridge.config.json` (git already ignores them).

## Fallback: your Mac's own Messages app

If Spectrum Cloud is down, the same bridge can run on a Mac through
`@spectrum-ts/imessage-local`. It reads the Mac's Messages database. It has
its own entrypoint and never shares one with the cloud provider.

* It needs a Mac signed in to Messages. `@spectrum-ts/imessage-local` uses
  `@photon-ai/imessage-kit` v3, which needs macOS 26 Tahoe. On older macOS,
  use the cloud bridge.
* Give your terminal **Full Disk Access** (System Settings > Privacy &
  Security > Full Disk Access). Then quit and reopen the terminal.
* The person texting must use a **different** Apple ID or phone than the
  Mac. Texting yourself does not work well. Use a second iPhone or a
  teammate's phone.

```bash
npm install @spectrum-ts/imessage-local     # Mac only; not in package.json
caffeinate -i npm run start:local
```

The allowlist, shop mapping, kill switch and limits are the same.

## Develop

```bash
npm run check      # lint (Biome), typecheck (tsc), tests (node --test)
```

The tests use a fake chat transport, a scripted Mimi API and a fake
converter. They never call Photon. `test/ffmpeg.test.ts` converts real files
when ffmpeg is installed and skips otherwise.

| File | What |
|---|---|
| `src/bridge.ts` | The rules: allowlist, dedupe, kill switch, limits, turns, ready-only replies, the approval poller |
| `src/mimi.ts` | Mimi API client: retries with backoff, timeouts, demo session, 202 polling |
| `src/media.ts` | File type checks and ffmpeg conversion |
| `src/spectrum.ts` | Spectrum messages to the bridge's own shape (no provider import) |
| `src/main-cloud.ts` | Entrypoint: Spectrum Cloud iMessage |
| `src/main-local.ts` | Entrypoint: the Mac's Messages app (optional) |
| `src/smoke.ts` | One fake message through the real API |

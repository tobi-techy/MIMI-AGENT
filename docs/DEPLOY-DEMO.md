# Hosting the judge demo (RAI-273, RAI-186)

**Prepared, not deployed.** Nothing here has been run against a host yet.
These are the steps to put the API on a public HTTPS address in demo mode
and the demo console on `https://demo.userail.money`. Each paid step and the
deploy itself needs the owner's yes.

What judges get: they open the console, it asks the API for a demo session,
and each visitor gets a fresh copy of the fictional Coral Lane Trading
world for an hour. All data is synthetic; model calls are real and capped.

```
judge's browser --> demo.userail.money (Cloudflare, static console)
                --> https://<api host> (this repo's Dockerfile, MIMI_MODE=demo)
                --> Meta Model API (Muse Spark 1.3, SAM 3.1), inside the naira caps
```

## 1. The API container

The repo's `Dockerfile` is the whole config: Python 3.12, listens on
`0.0.0.0:$PORT` (3000 by default), `GET /` answers `{"status":"ok"}` for
the health probe, and on start it migrates, seeds the synthetic world once
and serves. No secret is baked in; every value below is set in the host's
dashboard.

### Settings

| Variable | Value | Why |
|---|---|---|
| `MIMI_MODE` | `demo` | public demo sessions, per-visitor copies, caps |
| `PORT` | `3000` | the Dockerfile default; Atlasflow routes to 3000 whatever this says |
| `DATABASE_URL` | `sqlite:////tmp/mimi.db` | SQLite in `/tmp`: wiped on every restart or redeploy (see "What /tmp means") |
| `MIMI_MEDIA_DIR` | `/tmp/mimi-media` | console uploads, deleted with each copy |
| `MIMI_WORKERS` | `1` | one worker thread inside the API process; one replica only (SQLite) |
| `MIMI_DEMO_IP_DAILY_CAP_NAIRA` | `100` | approved: N100 of model spend per visitor address per Lagos day |
| `MIMI_DEMO_GLOBAL_DAILY_CAP_NAIRA` | `2000` | approved: N2,000 a day across every visitor |
| `MIMI_DEMO_SESSION_TTL_S` | `3600` | approved: one-hour sessions |
| `MIMI_DEMO_SESSIONS_PER_IP` | `20` | approved: 20 new sessions per address a day |
| `MIMI_DEMO_KILL` | `0` | the kill switch: `1` refuses every new turn (503), reads keep working |
| `MIMI_DEMO_TRUST_FORWARDED` | `1` only after step 4 below | read the visitor's address from the right-most `X-Forwarded-For` entry |
| `MIMI_DEMO_IP_SALT` | a new random secret (`python -c "import secrets;print(secrets.token_urlsafe(32))"`) | keys the address digests; without it a public dev salt is used |
| `MIMI_PHONE_PSEUDONYM_KEY` | a new random secret | keys thread ids for unknown numbers |
| `MIMI_CORS_ORIGINS` | `https://demo.userail.money` | the only browser origin allowed (localhost is always allowed) |
| `META_API_KEY` | the owner's pay-as-you-go Meta key | real Muse Spark and SAM 3.1 calls; unset means the offline model (holding replies, N0) |
| `MIMI_VOICE_REPLIES` | `all` | voice replies on the demo copies |
| `MIMI_TTS` | `fake`, or unset with `SPITCH_API_KEY` | `fake` plays a quiet tone and costs nothing. Real Spitch speech is billed per second and is **not counted in the naira caps**, so only with the owner's yes |
| `MIMI_RELEASE` | the deployed commit SHA | shown by `/v1/health` |

Leave `MIMI_ENV` unset (`dev`). `MIMI_ENV=production` refuses SQLite
(`mimi/api/config.py`, `Settings.check`), so the approved SQLite-in-`/tmp`
setup cannot run with it. That is why the salt and the pseudonym key are
set by hand above: dev mode would otherwise fall back to public dev keys.
The stricter route is Postgres (`DATABASE_URL=postgresql://...`) with
`MIMI_ENV=production`, which also refuses to start without the Meta key.

### What /tmp means

Every restart or redeploy starts from an empty store: open sessions end
(their tokens are no longer known, so the visitor starts a new copy), and **the day's spend counts
start again from zero**, so the caps reset with them. A host that restarts
the container five times in a day could spend up to N2,000 after each
restart. Keep it to one replica, do not redeploy during judging, and put a
spending limit on the Meta key itself as the outer guard.

### Checked offline on 30 Oct 2026

The same command the Dockerfile runs, with the values above and no Meta key,
on `127.0.0.1:3000`: `GET /` 200; `POST /v1/demo/sessions` 201 with a
3,600 s TTL and caps of 10,000 kobo per address and 200,000 kobo overall;
with `MIMI_DEMO_SESSIONS_PER_IP=2` the third session from one address was
429; with `MIMI_DEMO_KILL=1` a turn was 503 ("the demo is paused right now;
you can still look around") while `GET /v1/me` still answered; a CORS
preflight from `https://demo.userail.money` was allowed and one from another
origin was refused (400); about 100 MB of memory with three copies. The
docker image itself was not built here (no docker in the build sandbox). The
cap refusals (429) and the 403 on approving a credit raise are covered by
`tests/test_api_demo.py` in CI.

## 2. Atlasflow (the plan's pick)

**Price** (atlasflow.com/pricing, read 30 Oct 2026): Hobby is a $5 a month
minimum that comes back as $5 of usage credit; usage beyond it is metered by
the second. Single region, one replica, standard build machine.
**Blocker:** the saved Atlasflow login was rejected on 29 Oct; the owner
resets it from the Titan inbox and adds a card. Decide by Sat 31 Oct 12:00
WAT.

Atlasflow's contract matches the Dockerfile: it routes and health-checks
`0.0.0.0:3000`, default health path `/`, and builds from the repo's
`Dockerfile` (atlasflow.com/docs/deployments/runtime). Runtime variables are
set in the dashboard only (not the API or CLI) and apply on the next deploy
(atlasflow.com/docs/deployments/runtime/variables). The local filesystem is
disposable (atlasflow.com/docs/deployments/runtime/filesystem), which fits
SQLite in `/tmp`.

1. Push a branch `demo` pinned to the frozen commit (so merges to `main` do
   not redeploy mid-judging):
   `git push origin <frozen sha>:refs/heads/demo`.
2. Atlasflow dashboard: new project from GitHub repo `tobi-techy/MIMI-AGENT`.
   In the production environment set branch `demo`, root `/`, Dockerfile
   path `Dockerfile`, runtime tier `small`, minimum replicas 1.
3. Environment, Variables: add every row of the table above.
4. Deploy. Wait for the deployment to turn healthy (it probes `GET /`).
5. Copy the generated URL (`https://<project>.atlasflow.dev`). Optional:
   `atlasflow deployments domains add api-demo.userail.money` and add the
   DNS record it shows in Cloudflare.
6. Run the checks in section 5. Then set `MIMI_DEMO_TRUST_FORWARDED=1` and
   redeploy only if the forwarded-address check passes.

Rollback: redeploy the previous deployment from the dashboard, or set
`MIMI_DEMO_KILL=1` and redeploy to stop all spend while keeping the pages up.

## 3. Alternative: Railway

**Price** (railway.com/pricing and docs.railway.com/pricing, read 30 Oct
2026): new accounts get a 30-day free trial with a one-time $5 credit and
no card needed (2 vCPU and 1 GB a service during the trial); after that the
Free plan is $1 of credit a month (1 vCPU, 0.5 GB), and Hobby is a $5 a
month minimum with $5 of usage included. The trial covers the judging
window. Railway deploys a GitHub repo with a custom Dockerfile and gives a
`*.up.railway.app` HTTPS domain.

1. Sign in at railway.com with GitHub (the owner's own sign-up).
2. New project, Deploy from GitHub repo, `tobi-techy/MIMI-AGENT`, branch
   `demo` (as in Atlasflow step 1). Railway finds the `Dockerfile`.
3. Service, Variables: every row of the table above, including `PORT=3000`.
4. Service, Settings: health check path `/`; replicas 1; restart policy
   "on failure" (each restart resets the caps, see "What /tmp means").
5. Settings, Networking, Public Networking: Generate Domain, target port
   3000.
6. Run the checks in section 5, then decide `MIMI_DEMO_TRUST_FORWARDED`.

**Not recommended: Render's free web service.** It is free, but it spins
down after 15 minutes without traffic and takes about a minute to come back,
and its filesystem is lost on spin-down (render.com/docs/free). A judge who
pauses for 15 minutes loses their copy and the day's cap counts reset. Its
paid starter instance ($7 a month, render.com/pricing) does not spin down.

## 4. The console on Cloudflare (demo.userail.money)

The console is in its own repository (`mimi-webite`, private), with the
deploy already prepared in `deploy/demo-console/` (a Cloudflare Worker with
static assets only, custom domain `demo.userail.money`, no `workers.dev` or
preview URLs). Steps, from that repository's root, once the API has its URL:

1. `MIMI_DEMO_API_URL=https://<api host> npm run build:console-demo-cf`
   (refuses to run while the address is the placeholder; writes
   `out-console/` and its `_headers` with a CSP that allows only this site
   and the API).
2. `npx wrangler@4 deploy --config deploy/demo-console/wrangler.jsonc`, signed
   in to the Cloudflare account that holds the `userail.money` zone. The
   custom domain is created on the first deploy.
3. Open `https://demo.userail.money`: skeletons first, then "Live demo: your
   own copy of Coral Lane Trading".

Rollback: `npx wrangler@4 rollback --config deploy/demo-console/wrangler.jsonc`,
or remove the custom domain in the dashboard. The marketing site and
`console.userail.money` are not touched. Pin the console build to one commit
after the Mon 2 Nov 18:00 freeze.

## 5. Checks after each deploy

From outside (a laptop, not the host). `API=https://<api host>`.

```bash
curl -s $API/                                         # {"status":"ok"}
TOKEN=$(curl -s -X POST $API/v1/demo/sessions | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s $API/v1/me -H "Authorization: Bearer $TOKEN"   # mode demo, caps 10000 / 200000 kobo, paused false
curl -s -o /dev/null -w '%{http_code}\n' -X OPTIONS $API/v1/me \
  -H 'Origin: https://demo.userail.money' -H 'Access-Control-Request-Method: GET'   # 200
```

1. **Owner question:** `POST /v1/turns` with `{"message_id":"judge-1","source":"owner_message","text":"Who owes me the most?"}`.
   With the Meta key, Mimi names Mama Sola Provisions (N3,497,300 outstanding
   in the seeded book) and `GET /v1/me` shows a few naira spent.
2. **Forwarded address** (before turning on `MIMI_DEMO_TRUST_FORWARDED`):
   set `MIMI_DEMO_SESSIONS_PER_IP=2` for the test, open three sessions from
   one network (the third must be 429) and one from a second network such
   as a phone hotspot. If the second network is also 429, every visitor
   looks like the host's proxy: turn `MIMI_DEMO_TRUST_FORWARDED=1` on and
   repeat. If it still fails, the per-address cap is acting as a global cap;
   keep the global cap as the guard and say so in JUDGES.md. Put `20` back
   after.
3. **Kill switch:** set `MIMI_DEMO_KILL=1`, redeploy, a turn must be 503 and
   `GET /v1/threads` must still answer. Set it back to `0`.
4. **Credit raise refused:** approve a credit-review card in the console: it
   must be refused (403); decline works.
5. **Console:** a clean browser on `https://demo.userail.money`: new
   session, ask "Who owes me the most?", approve or decline a card, play a
   voice note, Reset demo.

Then replace the placeholder in `JUDGES.md` section 1 with the two links.

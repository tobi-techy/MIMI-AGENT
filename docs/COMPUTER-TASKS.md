# Computer tasks: guardrails, architecture and what works today

Status: prototype branch `feat/computer-tasks`. Nothing deployed, nothing merged.
Runtime: a **cloud browser** behind one seam. The provider is an implementation
detail: nothing in the API, the storage schema or the step log names it.

The product rule still governs: **Mimi proposes, code checks, the owner
approves, and Mimi never moves money.** Every guardrail below is enforced in
code, never by a model. **Every step is a real step**: a real browser, a real
page, a real screenshot, taken at the moment the step happened.

## 1. What works today, with numbers

One real run, through the same service the API uses
(`scripts/computer_smoke.py`, a `price_check` read of a public page):

| | |
|---|---|
| Status | `done`, `stop_reason` empty |
| Steps | 4, in plain words: open the page, take a screenshot, read the prices and promos, save what was found |
| Screenshots | 3 real PNGs at 1280x800, **189,835 bytes** each, `sha256 ed9aac41…35bc8f` |
| What it read | a **2,049-byte** text artifact of the page's own text, `sha256 8393636b…22a6b08` |
| Console polling | 64 to 74 polls; the step count grew 2 → 4 while the task ran, so the console can show steps as they appear |
| Live view | non-null while the session was open, `null` after it ended |
| Live view reachable without a provider login | **yes**, HTTP 200, `text/html`, the DevTools page (605 bytes) |
| Wall clock | 24 to 39 seconds per run, session create included |
| Spend | free tier; nothing was upgraded and no paid resource was created |

The same run is the reason two real bugs were fixed rather than assumed away:
the text a read produced reached the runner but was never persisted (so the
console would have shown screenshots only), and a browser failure wrote a task
status the API's own response model rejects (so the console would have seen a
500 exactly when the browser broke).

## 2. The live view, exactly

Every session gets a signed live view link. The engine reads it from the
provider's session debug endpoint and hands the console exactly this shape:

```
https://www.browserbase.com/devtools-fullscreen/inspector.html?wss=connect.browserbase.com/debug/<sessionId>/devtools/page/<pageId>?t=<signed token>&debug=true
```

* It is **linkable by a person who is not logged in to the provider**: a
  request with no provider credentials at all returned 200 and the DevTools
  page. `&navbar=false` hides the bar.
* The token is **per session** and dies with it. So the console must read the
  link while the task runs, and `GET /v1/computer/tasks/{id}` hands it out as
  `live_view_url` (see section 5). It is never stored.
* It is a **link, not an embeddable widget**. The console is a static export
  and holds no provider key, so it opens the link in a new tab. An iframe would
  need a server-side proxy that re-reads the link on each poll.
* It is a real screen, not a stream of stored frames: the browser session is
  live behind it.

## 3. The provider seam

`mimi/computer/engine.py` defines `ComputerPort` (`start`, `do`, `sign_in_link`,
`stop`). It is the only place in the codebase that talks to a browser provider.

* `mimi/computer/cloud_browser.py` is the shipped engine: one session per task,
  created with keep-alive, driven with Playwright over the session's CDP
  endpoint, released by us when the task ends. Configuration:
  `MIMI_CLOUD_BROWSER_API_KEY` and optionally `MIMI_CLOUD_BROWSER_BASE`. A
  project id is not needed and is not read from the environment anywhere.
* `mimi/computer/engine.py` also keeps the Maritime ports we built while
  evaluating that provider: `MaritimeBoxBrowser` (an existing Agent box with
  Chromium installed, screenshots pulled out through the files API) and
  `MaritimeDesktopBrowser` (a paid-plan desktop, which raises rather than
  pretending). `MIMI_COMPUTER_ENGINE=maritime|desktop` selects them.
* `FakeComputer` is the offline, scripted site the tests run against. It is
  never the shipped default.
* With no provider configured, `Unconfigured` refuses to run anything with a
  plain message: a task can still be proposed, and approving it says why it
  cannot run.

**Why a seam at all.** We had verified Maritime end to end before the runtime
changed: a real Linux desktop at 12.0 cents an hour, `available: true`, and an
Agent box running headless Chromium with a real 1280x800 PNG pulled out. This
account could not start either, though: it sits on an earlier seat plan
(`plan "free"`, `billingModel "seats"`, `includedMachines 3`, `machines 1`,
`maxAwakeMachines 1`), `POST /api/v1/computers` answered `402 computer_limit`
and both sandbox calls answered `402 plan_required`. The cloud browser needed
no plan change and no port exposed to anyone's laptop, so the runtime moved and
the Maritime work stayed behind the seam instead of being thrown away. Swapping
back is configuration, not a rewrite.

### What the free tier does not have

No proxies, no verified sessions and no automatic CAPTCHA solving. So
**bot-protected sites are out of scope, and say so rather than working around
them**: most large retailers, marketplaces and social platforms will refuse a
plain automated session. Steps are scripted and deterministic, and no
model-driven page reading (Stagehand-style) is used, so a task costs no model
tokens at all.

## 4. The five task kinds

The plan for each kind is built in code from the goal, the URL and the approved
domain. A kind has its own plan and its own guardrail profile, and a step its
profile does not allow is never built, so it can never run. **No kind has a
submit step**: a form is filled from the owner's own values and handed back as
a screenshot for the owner to submit themselves.

Published exactly once, by `GET /v1/computer/kinds`, so the console and the API
cannot disagree:

| `kind` | What it does | Plan | May fill | May download |
|---|---|---|---|---|
| `price_check` | Check a brand or supplier page for prices and promos | open, screenshot, read, finish | no | no |
| `order_list` | Pull the order list from a supplier page | open, screenshot, read, finish | no | yes |
| `bank_statement` | Download a statement from a bank page | open, screenshot, download, finish | no | yes |
| `gov_form` | Fill a form on a government or tax page and hand it back | open, screenshot, fill, screenshot, finish | yes | no |
| `generic` | Read a page on the approved domain | open, screenshot, read, finish | no | no |

An unknown `kind` falls back to `generic`.

## 5. The contract

```
POST /v1/computer/tasks              {goal, url, domain, kind, inputs} -> {task_id, status:"proposed", plan:[...]}
GET  /v1/computer/tasks              -> {tasks:[{task_id, goal, domain, status, created_at, step_count, stop_reason}]}
GET  /v1/computer/tasks/{id}         -> {task_id, status, kind, goal, url, domain,
                                         steps:[{index,action,code,url,note,screenshot}],
                                         artifacts:[{name,kind,mime,size,sha256}], stop_reason, note,
                                         created_at, started_at, finished_at, live_view_url}
POST /v1/computer/tasks/{id}/approve -> the same detail; the run continues in the background
POST /v1/computer/tasks/{id}/stop    -> the same detail; immediate, reason recorded
GET  /v1/computer/tasks/{id}/artifacts/{name} -> the bytes (PNG, CSV, PDF or text)
GET  /v1/computer/kinds              -> {kinds:[{kind, label}]}
```

Tasks are filtered by the caller's tenant, and every route needs an owner key.
The list is an honest empty list when there are none: nothing is seeded.
`live_view_url` is a `string | null`, populated while a session is open and
`null` once the task ends. It is deliberately **not** keyed to `status`: a task
paused at a sign-in wall is `awaiting_owner`, not `running`, and it keeps its
session and its link, because that is the state in which the owner signs in. So
the console's rule is "open it if it is not null".

### `action` is plain words, and `code` is ours

The console renders a step's `action` verbatim, so **`action` is always the
plain words a distributor can read** ("Open the page", "Take a screenshot",
"Read what the page shows", "Download the file", "Save what was found"), in
both the plan and the step log. The machine verb the runner dispatches on
(`goto`, `screenshot`, `extract`, `download`, `fill`, `finish`) lives in
`code`, a separate field **the console does not render**. A test walks every
rendered step and plan string and fails on anything shaped like a code
(an underscore-joined lowercase token) or carrying an internal id.

## 6. Where the browser runs: guardrails in code

| Requirement | Enforced in code |
|---|---|
| Domain allowlist, abort off-domain | `guardrails.host_allowed` before every navigation and against the observed URL after it; a mismatch ends the task `off_domain` |
| Never type credentials or a code | a password field, a sign-in form or an MFA prompt pauses the task `awaiting_owner`. Nothing is typed, and the step vocabulary has no action that could type one |
| Never buy, message, or change settings; never enter card data | a page scan ends the task `forbidden_page` on a checkout, a card field, a message box or a settings page. Those actions are not in the vocabulary at all |
| 10 minutes and 40 steps, then `stopped_runaway` | `GuardLimits(max_seconds=600, max_steps=40)`, wall clock from `mimi.clock.wall()`, checked before each step, consistent with `docs/LOOP-SAFETY.md` |
| Immediate stop, with the reason recorded | `/stop` calls `engine.stop` synchronously and writes `stopped_by_owner`; a task that ends on its own releases its session |
| Every step logged | one row per step: action, URL, a code-written note, and the name of a screenshot artifact |
| Redaction | `guardrails.redact` removes emails, phone numbers and long digit runs from a note. Prices stay readable |
| Nothing applied to the book | a task produces artifacts only. No book or Go write path is reachable from `mimi/computer` |

## 7. Architecture

```
mimi/computer/
  types.py         Pydantic models (extra="forbid"), the step vocabulary, stop reasons
  guardrails.py    pure checks: allowlist, page classifier, redaction, budgets
  plans.py         one plan and one profile per kind; the kind labels
  runner.py        runs an approved plan step by step, writes the step log and artifacts
  engine.py        ComputerPort, the Maritime ports, FakeComputer (tests only)
  cloud_browser.py the shipped engine: one cloud session per task, Playwright over CDP
  store.py         the task, its steps and its artifacts
  service.py       propose, approve, stop, read back; the engine is chosen here, from config
mimi/harness/computer.py   the same browser as one harness tool (section 8)
alembic/versions/0016_computer_tasks.py   computer_tasks, computer_steps, computer_artifacts
```

### What is stored, and where

* **`computer_tasks`**: the goal, the URL, the approved domain, the kind, the
  status, the plan, the owner's own input values, the limits, the engine's name,
  the stop reason and a code-written note.
* **`computer_steps`**: one row per step: the action, the URL, a **redacted**
  note, and the **name** of a screenshot artifact. The note is what the console
  shows; the model never writes it.
* **`computer_artifacts`**: the name, kind, mime type, size and `sha256` of each
  artifact. The bytes live on disk, under `MIMI_COMPUTER_DIR` (a temporary
  directory if that is unset), as `<dir>/<tenant>/<task_id>/<name>`.
* **Never stored**: a credential, a card number, a password, a code, or page
  text other than the artifact a read produced. **The signed live view link is
  never stored either**: it is held in memory while a session is open, because
  it dies with the session anyway.

### Signing in once

A page that asks for a sign-in pauses the task and keeps its session open, and
`live_view_url` hands the owner the live screen. **The owner signs in
themselves, in their own browser, through that link.** Mimi does not type a
password or a code, ever, and stores neither.

Reuse across tasks is designed but **not built**: the provider keeps a browser
context (cookies and login state) that a later session can attach to, which is
how "sign in once, then reuse it" would work. Today every task gets a fresh
session, so a portal behind a login needs the owner to sign in again, in that
task's own live view. That is the honest state, and it is called out again in
section 9.

## 8. How a helper reaches for the browser

The browser is a capability the whole team uses, not one owner-triggered flow.
It is one tool, `read_portal_page` (`mimi/harness/computer.py`, wired in
`mimi/api/helpers.py`):

* `fn(ctx, args) -> ReadPortalPageOut`, with `extra="forbid"` models, a
  one-paragraph docstring as its description, and registration through
  `registry.tool(kind="read", scope="org:owner", tier="auto",
  cost_tier="computer")`. It is listed in `policy.READ_TOOLS`, so Go's
  rulebook treats it as a read rather than as an action. A read needs no
  idempotency key and no dry run.
* It reads one page of a domain the owner approved, through the same service
  and the same guardrails as an owner-triggered task. It never signs in, buys,
  sends or changes anything, and it stops at the first sign of any of those.
* **What the page says arrives as data**: the text is defanged and wrapped in
  `<portal_page untrusted="true">`, so a page cannot close its own wrapper or
  pose as one of Mimi's records. Nothing it finds reaches the book: a figure
  the page published is the page's figure, and any change to a record goes to
  the owner for approval like every other write.
* With no browser configured, or a domain the owner has not approved, it
  refuses with a plain message and a next step, rather than pretending to have
  read something.
* **It is offered to helpers behind `MIMI_COMPUTER_TOOLS`, off by default**,
  like the check-in stage. Off, every helper's toolset is byte-identical, so no
  recorded answer is invalidated. On, Collector, Proof, Order, Order intake,
  Stock, Brand and Credit gain it, and their recorded loop answers must be
  re-recorded with `POLICY_VERSION` bumped. See section 9.

**How the step text reaches the console.** A step's words are written in code,
never by a model: the plan carries them ("Open the page", "Read what the page
shows") and the runner appends what the page turned out to be, so a logged step
reads "Read what the page shows: All products | Books to Scrape". `action` is
that plain phrase (the console renders it), `note` is the fuller sentence, and
the machine verb is in `code`, which the console ignores. Each step is persisted
as it finishes, so the console polls `GET /v1/computer/tasks/{id}` and watches
the step list grow (64 to 74 polls, 2 steps then 4, in the run above).
Screenshots and extracted text are served by name from the artifacts route. The
console labels the pictures honestly: a fresh screenshot, not a live stream. For
a live screen it opens `live_view_url`.

## 9. Real versus not available yet

**Real and tested.** Real cloud browser sessions with a real Chromium; real
per-step screenshots and real extracted text; the five kinds with their own
plans and profiles; the allowlist, the login-wall pause, the forbidden-page
stop, the budgets and the immediate stop; the five task routes plus the list,
the kinds and the artifact bytes; the harness tool (registered, tiered, tested
offline); the signed live view link, proven reachable with no provider login.

**Not available yet, plainly.**

* **A signed-in session reused across tasks.** The owner can sign in through a
  task's live view today, but the next task starts fresh. Keeping a provider
  context is the design, not the code.
* **A live view of a finished task.** The link dies with the session by design,
  so a task that is over has screenshots, not a screen.
* **Bot-protected sites.** No proxies, no CAPTCHA solving on the free tier, so
  large retailers, marketplaces and social platforms are out of scope.
* **Desktop GUI control, a shell, and files as an owner tool.** A browser
  session has no desktop; the Maritime engines that would give one are kept,
  unused, behind the seam.
* **Model-chosen steps.** The plan is code, and it stays code. Nothing here
  lets a model choose a click.

## 10. Open decisions

1. **Turn the harness switch on.** `MIMI_COMPUTER_TOOLS=on` is the owner's
   call: it needs `POLICY_VERSION` bumped and the affected helpers' recorded
   loop answers re-recorded (a model spend). Until then, no helper can reach
   the browser, even though the tool is built and tested.
2. **Which portals are in scope.** Non-bot-protected brand, supplier and bank
   pages are; the owner should name the first real one to try, with an account
   they are willing to sign into once by hand.
3. **Keep or drop the Maritime engines.** They cost nothing while unused and
   they are the fallback if a provider ever needs replacing.
4. **Whether the console wants an iframe.** A static export cannot hold a
   provider key, so an embedded live view needs a server-side proxy that
   re-reads the link on each poll.

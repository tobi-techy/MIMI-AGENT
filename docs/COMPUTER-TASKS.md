# Computer tasks: guardrails, architecture and feasibility

Status: prototype branch `feat/computer-tasks`. Nothing deployed, nothing merged.
Runtime: **Maritime** (https://maritime.sh) is the intended provider. This page
records what we tested against the live API and how the feature is built.

The product rule still governs: **Mimi proposes, code checks, the owner
approves, and Mimi never moves money.** Every guardrail below is enforced in
code, never by a model.

## 1. Verdict

* Maritime can do this, and it is the cheapest real option: a **Linux desktop**
  at **$0.12 an hour**, billed per second, with a **visible screen**, a
  **viewer link** the distributor watches, a shell, and an immediate stop.
* **This account cannot start one today.** The account is on an earlier
  **seat plan** (`plan: "free"`, `billingModel: "seats"`). Both Computers and
  Sandboxes are refused; only the included **Agent box** machines (3 included,
  1 in use) run.
* A **visible desktop screen with a viewer is only available on a paid
  Starter computer.** Plainly: no free-plan path gives a screen plus a live
  viewer. The `$20` Starter decision is the owner's.
* The cheapest **real** path that works on the current plan is an **Agent box
  running headless Chromium**: a real browser in a real container, real
  screenshots pulled out through the files API. It has **no visible screen and
  no viewer**. This is proven below, not assumed.
* The prototype is built against the **Computers** API shape (the intended
  path). It also carries an engine for the Agent box so a real run works today.

## 2. What we tested, with numbers

Live calls against `https://api.maritime.sh`, Bearer `mk_` key (Computers or
`manage` scope). The key is never printed, never committed, never in a fixture.

| # | Call | Result |
|---|------|--------|
| 1 | `GET /health` | `200 {"status":"ok","runtime":"fleet"}` |
| 2 | `GET /api/v1/account/limits` | `plan "free"`, `billingModel "seats"`, `includedMachines 3`, `machines 1`, `maxAwakeMachines 1`, `minMemMb 512`, `maxMemMb 8192`, `maxDiskGb 100` |
| 3 | `GET /api/billing/machine-types` | `computer` (Linux desktop) `available: true`, **12.0 cents/hour**; `windows` 18; `android` 15; `ephemeral_sandbox` 1.1; `persistent_sandbox` (Agent box) ~0.137 |
| 4 | `POST /api/v1/computers {externalUserId, name}` | `402 {"error":"computer_limit","message":"Computers need a paid plan. Upgrade to Starter to create one: https://maritime.sh/billing"}` |
| 5 | `POST /api/sandboxes/ephemeral {from:"base"}` | `402 plan_required` — "Sandboxes need a Maritime plan." |
| 6 | `POST /api/sandboxes/ephemeral {from:"python"}` | `402 plan_required` (same) |
| 7 | `POST /api/sandboxes/ephemeral {screen:"1280x800"}` | `400 screen_not_available` — "Use none or linux." |
| 8 | `POST /api/sandboxes/ephemeral {from:"base", screen:"linux"}` | `400 invalid_request` — "base has screen none." |
| 9 | `GET /api/v1/computers` | `{"computers":[]}` |
| 10 | agent `exec` in an Agent box | `exitCode 0`, `Debian GNU/Linux 12 (bookworm)`, `x86_64` |
| 11 | `apt-get install -y chromium` in an Agent box | `exitCode 0` |
| 12 | `chromium --headless=new --screenshot=/data/example.png https://example.com` | `exitCode 0`, `Chromium 156.0.8078.79`, a real **1280x800 PNG, 62,799 bytes** |
| 13 | files API download of that PNG | PNG 1280x800 8-bit RGB (viewed) |

So: the API shape is exactly what the contract needs, and a real browser in a
container is proven. What is missing is a **plan**, not a capability.

**Spend:** one ephemeral Browser Rendering request, one Agent box created and
deleted within minutes (per-second billing, ~$0.001/hr-scale), one Chromium
install. Total **well under $0.01**. No paid plan was started, nothing was
upgraded, no leftover machine remains (`GET /api/agents` is back to the single
existing agent).

## 3. What a Maritime "computer" is

Answers to the four questions, from the docs and the live API:

* **What it gives us.** A persistent **Linux desktop** micro-VM: XFCE, Chromium
  and LibreOffice, 2 vCPU and 4 GB, its own disk. It has a real **screen**,
  mouse and keyboard; a **shell** (`run_shell`/`exec`, 30 s per command); and
  **files** under `/data` and `/home/desk`. There is no root VM handle and no
  arbitrary container to shape: it is a desktop, driven by actions.
* **Creation and lifecycle.** **One computer per end user**, keyed by
  `externalUserId` (`POST /api/v1/computers`, idempotent: the same id returns
  the same desktop). It is created on first use, **sleeps after 5 idle
  minutes** with its memory snapshotted, and **wakes in about a second** on the
  next action. The snapshot is kept 7 days; the disk until the computer is
  deleted. There is no lifecycle for us to manage.
* **Cost on the cheapest tier.** **$0.12 an hour awake**, billed per second,
  no minimum; asleep, only disk at **$0.08 per GB-month**. It draws on the
  plan's usage credit (Starter $20 → $20 of credit). A 10-minute task costs
  about **$0.02**.
* **Screen, screenshots and stop.** `GET /api/v1/computers/{id}/screenshot`
  returns the frame (**1200x750** model frame of a 1280x800 desktop).
  `POST /api/v1/computers/{id}/viewer {"mode":"watch"}` mints a **view-only
  link** (10 minutes by default, 1 hour at most; a control link for a takeover;
  every link is revoked when the session closes, the computer sleeps, or a
  takeover completes). The stop is `POST /api/v1/computers/{id}/sessions/close`
  (`close_computer`, a 30 second grace) or `/sleep` immediately; `DELETE`
  destroys the disk. **Stop is a code call, not a model choice.**
* **Auth and clients.** Bearer `mk_` API key. Scopes: `computers` (a key that
  reaches only Computers and is refused everywhere else), or `manage`/full.
  Dashboard sessions cannot reach `/api/v1/computers`; it takes an API key.
  Clients: **MCP** (`https://mcp.maritime.sh/mcp`), **REST**
  (`/api/v1/computers`), the **CLI**, and SDKs — `maritime-sdk` for
  TypeScript/JS and `maritime` for Python, both zero-dependency. The Python
  SDK exposes `client.computers` for create/list/get/delete/wake/sleep;
  actions, screenshots, exec, files, the viewer and usage go through
  `client.request`. The TypeScript SDK also has a `computers/dialects` module
  that converts OpenAI, Gemini and Qwen computer-use actions to the canonical
  schema for models that do not speak MCP.

## 4. Mapping onto our contract and guardrails

| Requirement | How Maritime serves it | Enforced in code |
|---|---|---|
| One computer per task | `externalUserId = task id` → one desktop per task | runner passes the task id, never a model value |
| Domain allowlist, abort off-domain | `GET /screenshot` and the actions report the frame; the container shell reports the effective URL | `guardrails.host_allowed` before every `goto`; the observed URL is re-checked after, and a mismatch stops the task `off_domain` |
| Never type credentials or OTP; pause on a login wall | desktop stays visible; `POST /viewer` gives the owner a control link | the DOM is scanned for a password field; the task pauses `awaiting_owner` and hands over a viewer link. Moving money/typing passwords is not in the step vocabulary at all |
| Never purchase, message, change settings, enter card data | no such action exists in our vocabulary | page scan aborts `forbidden_page` on checkout, card fields, messaging or settings signals |
| 10 minutes / 40 steps → `stopped_runaway` | wall clock from `mimi.clock.wall()` | `GuardLimits(max_seconds=600, max_steps=40)` checked before each step |
| Immediate stop, recorded with why | `sessions/close` or `sleep` | `/stop` calls the engine synchronously and writes `stop_reason` |
| Every step logged: action, URL, note, screenshot | `GET /screenshot`, actions, exec | one step row per action, with a redacted note and a screenshot artifact |
| Redaction | text is never logged by Maritime either | `guardrails.redact` strips emails, phone numbers and digit runs before a note is stored |
| Nothing applied to the book | separate from the book | artifacts only; no book or Go write path is reachable from this module |

## 5. Architecture

New package `mimi/computer/`, plus one API module and one store table set.

```
mimi/computer/
  types.py       Pydantic models (extra="forbid"), step vocabulary, stop reasons
  guardrails.py  pure checks: allowlist, page classifier, redaction, budgets
  engine.py      ComputerPort; MaritimeDesktopEngine; MaritimeAgentBoxEngine; FakeComputer
  planner.py     propose(goal, url, domain) -> [PlanStep]  (code recipes; model later)
  runner.py      executes an approved plan under the guardrails, writes the step log
  store.py       SQL persistence for tasks, steps and artifacts
mimi/api/computer.py   the five endpoints (owner key only)
alembic/versions/0013_computer_tasks.py
```

### The engine port

`ComputerPort` is the seam between our guardrails and the provider:

```python
class ComputerPort(Protocol):
    def start(self, task: TaskRef) -> None: ...          # ensure the browser is ready
    def open(self, url: str) -> Page: ...                # navigate, observe
    def viewer(self) -> ViewerLink | None: ...           # live view for the owner
    def stop(self, reason: str) -> None: ...             # immediate
```

`Page` carries the effective URL, the title, a DOM excerpt and the screenshot
PNG. Three implementations:

* `MaritimeDesktopEngine` — the intended path. `POST /api/v1/computers`
  (`externalUserId = task id`), `POST /actions` for keyboard navigation,
  `GET /screenshot` for the frame, `POST /viewer` for the watch link,
  `POST /sessions/close` to stop. Needs Starter.
* `MaritimeAgentBoxEngine` — works on the current plan. Runs headless Chromium
  in an existing Agent box (`MIMI_MARITIME_AGENT_ID`) through `exec` and pulls
  the PNG and DOM through the files API. No screen, no viewer.
* `FakeComputer` — offline, in-memory, for the tests (the repo convention,
  like `FakeModel`). It is never the shipped default.

Selection is configuration (`MIMI_COMPUTER_ENGINE`), never a model decision.

### The five endpoints

```
POST /v1/computer/tasks              {goal, url, domain} -> {task_id, status:"proposed", plan:[...]}
GET  /v1/computer/tasks              -> {tasks:[{task_id, goal, domain, status, created_at, step_count, stop_reason}]}
POST /v1/computer/tasks/{id}/approve -> status "running" (also resumes a paused login handoff)
POST /v1/computer/tasks/{id}/stop    -> immediate stop, reason recorded
GET  /v1/computer/tasks/{id}         -> {task_id, status, steps:[{index,action,url,note,screenshot}], artifacts:[...], stop_reason}
```

Plus one additive sub-resource, `GET /v1/computer/tasks/{id}/artifacts/{name}`,
which serves the PNG bytes a step's `screenshot` field points at (the console
needs the bytes; the contract's step field stays a reference). Tasks are
filtered by tenant; the list is an honest empty list when there are none.

### Guardrails in code

* **Allowlist.** `host_allowed(url, domain)` accepts the approved domain and its
  subdomains only, and rejects anything else including IP literals and a
  different scheme. Checked before every navigation and against the observed
  URL after it.
* **Login wall.** A password input, a "sign in" form or an MFA prompt pauses
  the task `awaiting_owner` and returns a viewer link. Nothing is typed.
* **Forbidden page.** Checkout, card fields, "send message", account settings
  and payment signals abort the task `forbidden_page`. The step vocabulary has
  no action that could buy, message or change settings.
* **Budgets.** `GuardLimits(max_seconds=600, max_steps=40)`, wall clock read
  from `mimi.clock.wall()`. Exceeding either ends the task `stopped_runaway`
  with a code-written note, consistent with `docs/LOOP-SAFETY.md`.
* **Stop.** `/stop` calls `engine.stop` and records `stopped_by_owner`.
* **Redaction.** Step notes never carry credentials, card numbers, emails,
  phone numbers or long digit runs.

## 6. One task type, and variety

The prototype plans and runs one task type end to end (open a page on the
approved domain, capture the screenshot and the visible text as artifacts),
generated in code from `{goal, url, domain}`. The **step vocabulary is general**
(`goto`, `click`, `type`, `scroll`, `extract`, `screenshot`, `finish`), so a
richer plan runs through the same executor and the same guardrails; the owner
approves a plan once and code runs it. Model-proposed plans (a vision model
choosing clicks) are the next step, not this prototype.

## 7. Real versus not available yet

**Real.** Maritime REST calls for lifecycle, screenshots, actions, exec, files
and the viewer; a real Chromium in a real container producing real PNGs; the
guardrails; budgets; stop; the five endpoints; the step log and artifacts.

**Not available yet.**
* **Desktop GUI control** (mouse and keyboard on a visible desktop) and
  **shell/files as an owner tool**. Maritime's computer gives all of this; our
  prototype drives the browser engine only.
* **A live viewer** on the current plan: it exists only on the Computers route,
  which needs a paid plan. On the Agent box engine the field is `null`.
* **One container per task.** The free plan gives no sandbox and only 3 Agent
  boxes; the per-task computer turns on with a plan.
* **Model-driven computer use** (a vision model choosing coordinates).

## 8. Open decisions

1. Start **Starter** ($20/month, first 7 days free with a card) so the Linux
   desktop, the visible screen and the viewer turn on. This is the owner's call
   and the only thing blocking the intended design.
2. Until then, provision one Agent box as the browser container
   (`MIMI_MARITIME_AGENT_ID`), or accept that the live path is blocked.
3. Confirm the additive artifact endpoint for step screenshots.

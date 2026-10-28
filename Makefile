PY ?= python

.PHONY: credit-eval credit-eval-live credit-fixtures sim-credit e2e e2e-live e2e-postgres e2e-traced binding-eval binding-eval-live steer-eval steer-eval-live which-eval order-eval order-eval-live photo-route-eval photo-route-eval-live
.PHONY: loop-safety-eval claims-eval review-pocs install test eval eval-live eval-fake voice-eval voice-eval-live voice-fixtures voice-values-eval voice-values-eval-live voice-values-fixtures proof-eval proof-eval-live proof-fixtures stock-backtest stock-eval stock-eval-live stock-eval-fake sim-stock trace-stock sim sim-live sim-voice sim-voice-v2 sim-screens trace-smoke langfuse-setup langfuse-monitor langfuse-datasets proof-experiment voice-experiment receipts-experiment judge-calibration world-check lint typecheck secrets provenance migrate check harness-eval harness-eval-live trace-harness router-eval router-eval-live context-eval context-eval-live router-train trace-router api api-worker api-seed api-openapi api-openapi-check

install:            ## editable install with dev tools
	$(PY) -m pip install -e '.[dev,otel,langfuse]'

test:               ## full test suite, offline (FakeModel + recorded answers)
	META_API_KEY= SPITCH_API_KEY= $(PY) -m pytest

eval:               ## reply-reader eval, replaying the recorded Muse Spark answers
	META_API_KEY= $(PY) -m mimi.receipts.eval

eval-fake:          ## keyword FakeModel baseline
	$(PY) -m mimi.receipts.eval --fake

eval-live:          ## call Muse Spark (needs META_API_KEY); resumes from the cache
	$(PY) -m mimi.receipts.eval --live

voice-eval:         ## voice-note eval, replaying recorded transcripts and verdicts
	META_API_KEY= SPITCH_API_KEY= $(PY) -m mimi.voice.eval

voice-eval-live:    ## call Spitch, Muse Voice Transcribe and Muse Spark (needs both keys)
	$(PY) -m mimi.voice.eval --live

voice-fixtures:     ## make missing synthetic clips with Spitch TTS (needs SPITCH_API_KEY)
	$(PY) scripts/make_voice_fixtures.py

voice-values-eval:  ## amounts and dates from 50 voice clips, replaying recorded transcripts and voice replies
	META_API_KEY= SPITCH_API_KEY= $(PY) -m mimi.receipts.values_eval --no-write

voice-values-eval-live: ## record missing transcripts and voice replies (needs both keys); writes the report
	$(PY) -m mimi.receipts.values_eval --live

voice-values-fixtures: ## make missing amounts-and-dates clips with Spitch TTS (needs SPITCH_API_KEY)
	$(PY) scripts/make_voice_values_fixtures.py

proof-eval:         ## payment-screenshot eval, replaying the recorded Muse Spark answers
	META_API_KEY= $(PY) -m mimi.proof.eval

proof-eval-live:    ## call Muse Spark on the 40 screenshots (needs META_API_KEY); resumes from the cache
	$(PY) -m mimi.proof.eval --live

proof-fixtures:     ## redraw the 40 synthetic screenshots and their labels (Pillow)
	$(PY) scripts/make_proof_fixtures.py

stock-backtest:     ## stock estimator backtest with the one-tap loop (no model; compares to the report)
	$(PY) -m mimi.stock.backtest --strict

stock-eval:         ## stock answer-reading eval, 30 cases, replaying the recorded Muse Spark answers
	META_API_KEY= $(PY) -m mimi.stock.eval

stock-eval-fake:    ## keyword FakeModel run of the same 30 cases (plumbing)
	$(PY) -m mimi.stock.eval --fake

stock-eval-live:    ## call Muse Spark (needs META_API_KEY); resumes from the cache
	$(PY) -m mimi.stock.eval --live

sim-stock:          ## offline low-stock scene: Monday stock check, one-tap counts, restock drafts
	META_API_KEY= $(PY) -m mimi.sim --stock --transcript

sim:                ## offline one-day sim, 3 shops, with the transcript
	META_API_KEY= $(PY) -m mimi.sim --shops 3 --transcript

sim-live:           ## the same with real Muse Spark calls (needs META_API_KEY)
	$(PY) -m mimi.sim --shops 3 --transcript

sim-voice:          ## offline sim where every shop answers with a synthetic voice note
	META_API_KEY= SPITCH_API_KEY= $(PY) -m mimi.sim --shops 3 --transcript --voice 1

world-check:        ## the committed world fixture matches its seed
	$(PY) -m mimi.world --check

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy

secrets:            ## fail on any secret not in .secrets.baseline
	git ls-files -z | xargs -0 detect-secrets-hook --baseline .secrets.baseline  # pragma: allowlist secret

provenance:         ## every commit was made in the hackathon window
	$(PY) scripts/hackathon_provenance.py

migrate:            ## create Mimi's tables; set MIMI_DB_URL first
	alembic upgrade head

check: lint typecheck test review-pocs loop-safety-eval claims-eval api-openapi-check eval voice-eval voice-values-eval proof-eval stock-backtest stock-eval harness-eval router-eval context-eval credit-eval e2e binding-eval which-eval steer-eval order-eval photo-route-eval world-check secrets provenance

sim-voice-v2:       ## offline sim: voice notes, payment promises, voice replies
	META_API_KEY= SPITCH_API_KEY= $(PY) -m mimi.sim --shops 3 --transcript --voice 1 --promises 1 --voice-replies

sim-screens:        ## offline sim: shops pay by screenshot, some fake, one resend
	META_API_KEY= $(PY) -m mimi.sim --shop-ids shop-019,shop-037,shop-025 --transcript --screenshots 1 --fakes 0.4

# --- Langfuse (optional; every target needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY) ---

trace-smoke:        ## 3-shop screenshot sim traced to Langfuse (demo env); replays recorded answers on a rerun
	$(PY) scripts/trace_smoke.py --public

langfuse-setup:     ## model prices, code evaluator + rule, LLM judge (disabled), monitor dashboard
	$(PY) scripts/langfuse_setup.py

langfuse-monitor:   ## the five monitor checks over the last 24 h (exit 1 on a breach)
	$(PY) scripts/langfuse_monitor.py --hours 24

langfuse-datasets:  ## upload the three eval sets as Langfuse datasets (synthetic media included)
	$(PY) -m mimi.observe.experiments upload all

proof-experiment:   ## proof eval as a Langfuse experiment (replayed answers; add JUDGE=1 for the reply judge)
	$(PY) -m mimi.observe.experiments run proof $(if $(JUDGE),--judge,)

voice-experiment:   ## voice eval as a Langfuse experiment (replayed transcripts and verdicts)
	$(PY) -m mimi.observe.experiments run voice

receipts-experiment: ## reply-reader eval as a Langfuse experiment (replayed answers)
	$(PY) -m mimi.observe.experiments run receipts

trace-stock:        ## low-stock scene traced to Langfuse (demo env, shops type their answers); --public link
	$(PY) scripts/trace_stock.py --public

judge-calibration:  ## calibrate the reply-tone LLM judge on 8 labelled replies (Muse, cached)
	$(PY) -m mimi.observe.experiments calibrate-judge

# --- Agent harness (RAI-278, RAI-279) ---

review-pocs:        ## the outside review's proofs of concept (each reproduced on main before its fix); all must hold
	META_API_KEY= $(PY) -m mimi.harness.review_pocs

loop-safety-eval:   ## scripted adversarial runs (looping model, flapping tool, ping-pong helpers, self-scheduling routine, poison jobs); every attack stopped within limits (docs/LOOP-SAFETY.md)
	META_API_KEY= $(PY) -m mimi.api.loop_safety_eval --no-write

claims-eval:        ## the verification gate: 16 unbacked figures, dates, names, done claims, promises and money claims held, 13 look-alike controls sent (docs/LOOP-SAFETY.md)
	META_API_KEY= $(PY) -m mimi.harness.claims_eval --no-write

harness-eval:       ## every harness suite offline: 50 Collector scenarios (scripted + recorded Muse Spark), security, retries, runaway budgets, idle
	META_API_KEY= $(PY) -m mimi.harness.eval

harness-eval-live:  ## re-record Muse Spark on the 50 Collector scenarios (needs META_API_KEY); then run harness-eval
	$(PY) -m mimi.harness.eval --live --record-only

trace-harness:      ## four replayed Collector runs traced to Langfuse (demo env; no model cost); --public link
	$(PY) scripts/trace_harness.py --public

# --- Router and context builder (RAI-280, RAI-281) ---

router-eval:        ## router on 300 labelled messages, replaying recorded Muse Spark answers; pass marks
	META_API_KEY= $(PY) -m mimi.harness.router_eval

router-eval-live:   ## record the router's Muse Spark calls (needs META_API_KEY; about N520 from empty)
	$(PY) -m mimi.harness.router_eval --live

context-eval:       ## context builder vs full history on the 50 Collector scenarios, replayed; pass marks
	META_API_KEY= $(PY) -m mimi.harness.context_eval

context-eval-live:  ## record both arms (needs META_API_KEY; about N1,200 builder + N5,300 baseline from empty; resumable)
	$(PY) -m mimi.harness.context_eval --live

router-train:       ## regenerate the fast classifier's synthetic training set (contributor tier; resumable)
	$(PY) scripts/make_router_train.py

trace-router:       ## eight routing decisions and four context-built runs traced to Langfuse (replayed; no model cost)
	$(PY) scripts/trace_router_context.py --public

# --- Credit helper (RAI-271) ---

credit-eval:        ## shelf counts (24 synthetic photos + 6 stress), recommendations, 10 conversations, routing; recorded SAM 3.1 and Muse Spark; pass marks
	META_API_KEY= $(PY) -m mimi.credit.eval

credit-eval-live:   ## record what is missing (needs META_API_KEY; about N800 from empty)
	$(PY) -m mimi.credit.eval --live

credit-fixtures:    ## redraw the synthetic shelf photos and labels (new bytes: re-record SAM after)
	$(PY) scripts/make_shelf_fixtures.py

sim-credit:         ## offline credit-bump scene: ask, shelf photo, SAM 3.1 count, approvals draft
	META_API_KEY= $(PY) -m mimi.sim --credit --transcript

# --- Agent HTTP API and durable store (RAI-266) ---
# Local demo keys only; set MIMI_API_KEYS (and META_API_KEY for real Muse Spark) yourself.
# With MIMI_ENV=production nothing is filled in: DATABASE_URL and MIMI_API_KEYS must be set.
API_ENV = case "$${MIMI_ENV:-dev}" in prod|production|PROD|PRODUCTION|Production) \
	if [ -z "$${DATABASE_URL:-$${MIMI_DB_URL:-}}" ] || [ -z "$${MIMI_API_KEYS:-}" ]; then \
	echo "MIMI_ENV=production: set DATABASE_URL and MIMI_API_KEYS (no SQLite or demo key defaults)" >&2; \
	exit 1; fi;; esac; \
	DATABASE_URL=$${DATABASE_URL:-sqlite:///./mimi.db}; export DATABASE_URL; \
	MIMI_API_KEYS=$${MIMI_API_KEYS:-coral-lane:owner:local-owner-key-0001,coral-lane:channel:local-channel-key-001}; \
	export MIMI_API_KEYS;

api-seed:           ## migrate and load the synthetic Coral Lane world once (DATABASE_URL, default sqlite:///./mimi.db)
	@$(API_ENV) alembic upgrade head && $(PY) -m mimi.store seed --if-empty

api: api-seed       ## the HTTP API on :8000 with one worker thread (offline model without META_API_KEY)
	@$(API_ENV) $(PY) -m mimi.api serve --host 127.0.0.1 --port $${PORT:-8000}

api-worker:         ## an extra worker process for the API's turn queue (safe to kill)
	@$(API_ENV) $(PY) -m mimi.api worker

api-openapi:        ## regenerate docs/api/openapi.json
	$(PY) -m mimi.api openapi

api-openapi-check:  ## fail when docs/api/openapi.json is stale
	$(PY) -m mimi.api openapi --check

# --- End-to-end eval (RAI-272) ---

e2e:                ## 10 simulated shops through the real API, store and worker processes; replays recorded Muse Spark, SAM 3.1 and persona answers; pass marks
	META_API_KEY= $(PY) -m mimi.api.e2e --no-write

e2e-postgres:       ## the same e2e on Postgres (set E2E_DB to a throwaway database: it is wiped)
	META_API_KEY= $(PY) -m mimi.api.e2e --no-write --db "$${E2E_DB:?set E2E_DB}"

e2e-traced:         ## replay the e2e with Langfuse on (needs LANGFUSE_* keys; environment e2e, one trace per job)
	META_API_KEY= MIMI_TRACE_RUN=$${MIMI_TRACE_RUN:-e2e-$$(date +%s)} $(PY) -m mimi.api.e2e --no-write

which-eval:         ## two open asks on one thread: 40 held-out replies, code only; 95% and 0 ambiguous binds
	META_API_KEY= $(PY) -m mimi.api.which_eval --no-write

binding-eval:       ## pending-ask binding: 43 held-out replies to receipt asks, replaying recorded Muse Spark answers; 95% and 0 false consents
	META_API_KEY= $(PY) -m mimi.api.binding_eval --no-write

binding-eval-live:  ## record the binding eval's Muse Spark calls (needs META_API_KEY; about N350 from empty)
	$(PY) -m mimi.api.binding_eval --live

e2e-live:           ## record what is missing (needs META_API_KEY; about N600 from empty) and write eval/e2e/e2e.report.*
	$(PY) -m mimi.api.e2e --live

# --- Steering: every part of every message, even mid-task (RAI-301) ---

steer-eval:         ## 53 held-out cases (multi-part, mid-task, corrections, voice, screenshots, photos, documents), replaying recorded Muse Spark answers; 90% and zero lost, duplicate, money or decided edits
	META_API_KEY= $(PY) -m mimi.api.steer_eval --no-write

steer-eval-live:    ## record the steer eval's Muse Spark calls (needs META_API_KEY; about N390 from empty)
	$(PY) -m mimi.api.steer_eval --live

order-eval:         ## order intake: 47 held-out orders (typed, voice, list photos, edits, credit), replaying recorded Muse Spark answers; 90% and zero invented, credit or decided edits
	META_API_KEY= $(PY) -m mimi.api.order_eval --no-write

order-eval-live:    ## record the order eval's Muse Spark calls (needs META_API_KEY; about N640 from empty)
	$(PY) -m mimi.api.order_eval --live

photo-route-eval:   ## photo routing: 27 fresh list photos and look-alikes, replaying recorded Muse Spark answers; 90% and zero look-alikes to Order
	META_API_KEY= $(PY) -m mimi.api.photo_route_eval --no-write

photo-route-eval-live: ## record the photo routing eval's Muse Spark calls (needs META_API_KEY; about N180 from empty)
	$(PY) -m mimi.api.photo_route_eval --live

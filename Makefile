PY ?= python

.PHONY: install test eval eval-live eval-fake voice-eval voice-eval-live voice-fixtures proof-eval proof-eval-live proof-fixtures sim sim-live sim-voice sim-voice-v2 sim-screens trace-smoke langfuse-setup langfuse-monitor langfuse-datasets proof-experiment voice-experiment receipts-experiment judge-calibration world-check lint typecheck secrets provenance migrate check harness-eval harness-eval-live trace-harness router-eval router-eval-live context-eval context-eval-live router-train trace-router

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

proof-eval:         ## payment-screenshot eval, replaying the recorded Muse Spark answers
	META_API_KEY= $(PY) -m mimi.proof.eval

proof-eval-live:    ## call Muse Spark on the 40 screenshots (needs META_API_KEY); resumes from the cache
	$(PY) -m mimi.proof.eval --live

proof-fixtures:     ## redraw the 40 synthetic screenshots and their labels (Pillow)
	$(PY) scripts/make_proof_fixtures.py

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

check: lint typecheck test eval voice-eval proof-eval harness-eval router-eval context-eval world-check secrets provenance

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

judge-calibration:  ## calibrate the reply-tone LLM judge on 8 labelled replies (Muse, cached)
	$(PY) -m mimi.observe.experiments calibrate-judge

# --- Agent harness (RAI-278, RAI-279) ---

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

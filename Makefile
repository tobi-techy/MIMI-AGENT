PY ?= python

.PHONY: install test eval eval-live eval-fake voice-eval voice-eval-live voice-fixtures proof-eval proof-eval-live proof-fixtures sim sim-live sim-voice sim-voice-v2 sim-screens world-check lint typecheck secrets provenance migrate check

install:            ## editable install with dev tools
	$(PY) -m pip install -e '.[dev,otel]'

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

check: lint typecheck test eval voice-eval proof-eval world-check secrets provenance

sim-voice-v2:       ## offline sim: voice notes, payment promises, voice replies
	META_API_KEY= SPITCH_API_KEY= $(PY) -m mimi.sim --shops 3 --transcript --voice 1 --promises 1 --voice-replies

sim-screens:        ## offline sim: shops pay by screenshot, some fake, one resend
	META_API_KEY= $(PY) -m mimi.sim --shop-ids shop-019,shop-037,shop-025 --transcript --screenshots 1 --fakes 0.4

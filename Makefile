PY ?= python

.PHONY: install test eval eval-live eval-fake voice-eval voice-eval-live voice-fixtures sim sim-live sim-voice world-check lint typecheck secrets provenance migrate check

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

check: lint typecheck test eval voice-eval world-check secrets provenance

# Mimi agent API (RAI-266). Demo data is synthetic; model calls are real when
# META_API_KEY is set. No secret is baked in: pass them at run time.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite:////data/mimi.db \
    MIMI_WORKERS=1 \
    MIMI_SEED_DEMO=1 \
    PORT=3000 \
    MIMI_SKILLS_DIR=/app/skills \
    MIMI_SKILL_DRAFTS_DIR=/data/skill-drafts \
    MIMI_FIXES_2026_10_30=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY mimi ./mimi
# Skills (RAI-314, used when MIMI_SKILLS=1): vetted folders, locked by hash.
COPY skills ./skills
COPY alembic ./alembic
RUN pip install '.[postgres]' \
    && useradd --system --uid 10001 mimi \
    && mkdir -p /data && chown mimi /data

USER mimi
VOLUME ["/data"]
# The platform (Atlasflow) expects 0.0.0.0:3000 and probes GET / there.
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD sh -c 'python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=4)" "http://127.0.0.1:${PORT}/"'

CMD ["sh", "-c", "alembic upgrade head && if [ \"$MIMI_SEED_DEMO\" = 1 ]; then python -m mimi.store seed --if-empty; fi && exec python -m mimi.api serve --host 0.0.0.0 --port \"$PORT\""]

# ops/prometheus: scraping Mimi's metrics

`mimi.observe.metrics` serves Prometheus text on one path with no framework
and no vendor. This folder is the scrape and alert configuration for the
pilot; the numbers themselves are in `docs/SLO.md`.

## Serve it

The endpoint is an ASGI app. Mount it in the API process, or run it beside
the workers from the queue's own store:

```bash
# standalone, for a pilot box (one line of summary on stderr, the page on GET)
MIMI_DB_URL=postgresql://... MIMI_METRICS_TOKEN=... \
  python -m mimi.observe.metrics --listen 0.0.0.0:9101
# local check, no server: prints the summary, then the exposition
MIMI_DB_URL=postgresql://... python -m mimi.observe.metrics
```

`MIMI_DB_URL` (or `DATABASE_URL`) is the store the API and the workers use.

```python
# in an existing ASGI app, if the API should serve it itself
from mimi.observe.metrics import asgi_app
app.mount("/metrics", asgi_app(engine, path="/"))
```

It refuses to serve without a token. Set `MIMI_METRICS_TOKEN` in the
environment and scrape with the same value as a bearer token. Only
`MIMI_METRICS_OPEN=1` opens it, and that is for a private network with no
scraper credential to spare, never for a public host: the exposition carries
tenant ids.

## Scrape it

```yaml
# prometheus.yml
rule_files:
  - /etc/prometheus/mimi.rules.yml

scrape_configs:
  - job_name: mimi
    scrape_interval: 30s
    scrape_timeout: 5s
    scheme: http
    authorization:
      credentials_file: /etc/prometheus/mimi-token
    static_configs:
      - targets: ["mimi-api.internal:9101"]
```

Copy `mimi.rules.yml` next to the rest of the rule files and reload
Prometheus. The rules reference only the series this repository emits;
`tests/test_slo.py` fails if that stops being true.

## Severities

| Severity | Meaning | Who acts |
|---|---|---|
| `page` | A person is waiting now, or the page itself is blind | on-call engineer, immediately |
| `warning` | The objective is drifting; a restart or a decision fixes it | on-call engineer, same working day |
| `ticket` | The owner's decision or backlog, not a fault | founder (operations), within the SLO's window |

Routing (PagerDuty, Opsgenie, a phone) is deployment configuration and is not
in this repository. Each rule carries `slo` and a `runbook` link so any
receiver can find its first action.

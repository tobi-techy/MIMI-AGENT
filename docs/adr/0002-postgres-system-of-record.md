# ADR 0002: Postgres as the system of record, with the queue in it

* Status: accepted
* Date: 10 Nov 2026
* Supersedes: none

## Context

The demo runs SQLite (`sqlite:///./mimi.db`), and the durable store, threads,
turn queue, approvals and the world all live in that one database. Production
already refuses SQLite (`Settings.check`). The turn queue is a table in the same
database; at scale, claiming rows is a hot spot and the queue competes with reads
(`ARCHITECTURE-TARGET.md`, Gap 2).

Alternatives considered:

* **A dedicated broker** (SQS/RabbitMQ/Redis) for the turn queue.
* **Postgres for data, a managed queue for work.**

## Decision

**Postgres is the system of record.** The **queue lives in Postgres** in a jobs
table, claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, so a claim is
transactional with the state change it belongs to and a killed worker's turn is
taken over by another worker from its checkpoint. Our own low-volume outbound
work stays here.

Idempotency keys on side effects (already generated) are enforced at the
boundary, not only stored. A **managed queue for inbound webhooks** stays a
future option, not a Phase 0 requirement, because inbound is bursty and
third-party-driven while our own work is not.

## Consequences

* One datastore to operate, back up, restore and reason about; no broker to run,
  no dual-write between a broker and the database.
* Claim semantics lean on Postgres and must stay bounded (attempts, backoff,
  dead-letter with replay — Phase 0).
* Partitioning the hot tables, pooling and per-tenant concurrency caps become
  the scale levers here, with triggers in ADR 0005.
* Revisit if inbound webhook volume or fan-out outgrows `SKIP LOCKED`, or if a
  managed queue becomes cheaper to run than the tuning it replaces.

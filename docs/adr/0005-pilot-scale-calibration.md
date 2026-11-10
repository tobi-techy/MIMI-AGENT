# ADR 0005: Calibrate to pilot scale, with triggers that force revisiting it

* Status: accepted
* Date: 10 Nov 2026
* Supersedes: none

## Context

The product target is large ("a platform for millions"), but the owner's
decision (10 Nov 2026) is to **engineer for pilot scale now**: up to 10,000
shops, tens of distributors, not millions. The target doc lists the real scale
gaps (partitioning, sharding, connection budget, staging, canary, restore, load
test). Building all of that now would slow the work that makes the pilot real;
building none of it would leave the first serious customer on a demo.

## Decision

**Do the cheap, reversible scale work now; make the expensive work a triggered
decision.**

Do now (Phase 0):

* Enforce boundaries in the build (ADR 0001); keep one deployable plus workers.
* Postgres as the system of record and queue (ADR 0002).
* Index the hot queries, bound the connection pool, cap workers and per-tenant
  concurrency and cost, so one distributor cannot starve the rest.
* A staging environment that mirrors production, a canary tenant, a rehearsed
  rollback and a rehearsed restore.
* A load test at pilot scale with a recorded p95 latency and queue lag, so "it
  scales" is a number, not a claim.

Defer, with triggers:

| Defer | Revisit when |
|---|---|
| Time-partition hot tables (`inbound_messages`, `turns`, `messages`) | a hot table passes ~50M rows or ~100GB, or a maintenance/vacuum window exceeds its budget |
| Tenant sharding (shard key in every table and ID) | one Postgres primary cannot meet the latency budget, or a tenant asks for isolation |
| Managed queue for inbound webhooks | inbound volume or burst shape outgrows `SKIP LOCKED` |
| Multi-region | a customer commitment requires it |
| Service extraction | ADR 0001's revisit condition (team ownership diverges or a helper must scale independently) |
| Data warehouse | analytics load starts to affect production latency |

Also revisit the calibration itself when: p95 latency or queue lag sits over its
SLO for a week; a single tenant exceeds ~30% of total load; sustained usage
crosses ~80% of the documented capacity number; or a restore/canary drill fails.

## Consequences

* Work now is small and reversible; partitioning and sharding are not done
  speculatively.
* The triggers are written down, so the next step is a decision with evidence,
  not a surprise in an incident.
* "It scales" is bounded by a stated capacity number; past that number the
  triggers above apply.
* Revisit the numbers themselves as real usage data replaces the estimates.

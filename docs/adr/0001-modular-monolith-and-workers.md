# ADR 0001: One deployable with enforced module boundaries, plus worker processes

* Status: accepted
* Date: 10 Nov 2026
* Supersedes: none

## Context

Mimi grew from a hackathon demo into 20 packages and roughly 300 modules in
`mimi/`. Two things are true at once: the agent spine (loop, tool registry,
policy tiers, claims verification, loop safety) is already strong and must not
be rewritten, and the whole thing is one product with one owner, not a fleet of
teams. The owner's decision (10 Nov 2026) is to engineer for pilot scale
(up to 10,000 shops, tens of distributors) and to keep the "best shape that is
easy to run".

Alternatives considered:

* **Microservices per helper** (credit service, stock service, proof service):
  independent scaling and blast radius, at the cost of network hops, distributed
  transactions across the money-adjacent paths, and a deployment surface a small
  team cannot run.
* **A plain monolith with no enforced boundaries**: simplest to run, but the
  boundaries hold only by habit; with agents and humans contributing in parallel,
  that decays.

## Decision

Ship **one deployable** (one image, one HTTP process) with **enforced module
boundaries**, and scale work with **separate worker processes** off the same
store. Boundaries are rules in the build (`tests/test_architecture.py`,
`docs/ARCHITECTURE.md`), so a crossing fails CI rather than review.

The API process serves HTTP; `mimi.api worker` processes run turns, claimed from
the durable queue with leases so a killed worker loses no work. Both run the same
code and the same package layering.

## Consequences

* One image to build, deploy, roll back and reason about; no service discovery,
  no cross-service transaction.
* A hot path is optimised in place first; extraction is a deliberate, later
  decision, not the default.
* Scale-out is "run more workers" and "add a replica", bounded by the database.
* Boundaries cost a test and a package-map row per new package; that cost is the
  point.
* Revisit when team ownership genuinely diverges (independent deploy cadence,
  isolation or compliance needs) or when one helper must scale or fail
  independently. Until then, services would buy coordination we do not need.

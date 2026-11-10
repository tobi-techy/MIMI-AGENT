# ADR 0003: Policy-tiered tools, with code doing every sum

* Status: accepted
* Date: 10 Nov 2026
* Supersedes: none

## Context

Mimi is a set of WhatsApp agents that read shop messages and act on a
distributor's book. The model reads and judges; it must never write a figure or
move money, and a wrong number to a shop or a wrong write to the books is the
failure mode that ends trust. The spine already implements this: a typed tool
registry with policy tiers, integer-kobo sums in code, a claims verification
gate, and loop-safety limits.

Alternatives considered:

* **Let the model compute and call tools freely** with a prompt asking it to be
  careful.
* **Give each helper its own bespoke guardrail** rather than one shared spine.

## Decision

Keep and extend the **shared spine**:

* Every tool carries a **policy tier** (never / ask / auto) assigned by **code**,
  never by the model. Moving money or changing bank details is `never` for every
  helper and cannot be put in a toolset.
* **Code does every sum and every check**: money is integer kobo, figures come
  from records or the owner's own words, and a claim that money moved is always
  held.
* **Write tools return intents only**; the loop never writes to the book.
  Approval, idempotency, audit and undo stay in Mimi code.
* A verification gate checks figures, dates, names and "done" claims before any
  text reaches a person or helper.

## Consequences

* The model cannot be trusted with arithmetic by construction; a new tool must
  register a tier and a dry run or registration fails.
* Some interactions are slower (an approval step) — that is the product, not the
  cost.
* The demo path and every eval replay the same guards, so a change to a prompt,
  tool description or schema re-records the evals.
* Revisit only the *shape* (new tiers, more helpers) — never the invariant that
  the model proposes and code checks.

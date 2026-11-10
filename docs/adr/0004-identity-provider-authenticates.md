# ADR 0004: A provider authenticates, we authorise

* Status: accepted
* Date: 10 Nov 2026
* Supersedes: none

## Context

There is no user model today: tenants and demo keys in configuration. The target
product has owners, supervisors, reps and brand viewers, scoped to a distributor,
and brand users who see aggregates without a distributor's customer data.
The owner's decision (10 Nov 2026): a provider owns **authentication** (phone OTP
as a first-class identifier, plus email and Google); we own the **domain user
model**, roles and permissions.

Alternatives considered:

* **Own the credentials** (store password hashes and OTP state ourselves).
* **Provider owns everything**, including roles.

## Decision

**Split the responsibility at the boundary:**

* The provider verifies an identifier (phone, email, Google) and returns a
  subject. We do not store credentials or run password/OTP logic.
* We own the domain model: a `user` keyed to the provider subject, **roles**
  (owner, supervisor, rep, brand viewer) scoped to a **distributor**, and the
  permissions each role has over the book.
* On first successful authentication we **provision just-in-time**: create or map
  the user and its roles in our database, then issue our own session.
* Our database is the **source of truth for authorisation**; the provider's
  subject is an identity, not a permission.
* Phone is a first-class identifier because the product's channel is WhatsApp.

## Consequences

* We can change providers without a user migration, and an identity provider
  outage does not decide who can see what.
* We must build the users/roles tables, session issuance and the mapping from
  subject to user; roles stay testable and ownable by us.
* A brand viewer's scope (aggregates only, no distributor customer data) is our
  rule to enforce, not the provider's.
* Revisit the provider choice freely; revisit the *split* only if we decide to
  outsource authorisation, which would trade control of the money-adjacent
  permissions for less code.

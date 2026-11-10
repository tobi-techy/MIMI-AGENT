# Identity, roles and authorisation

Phase 1 slice 1. Who is calling, what they may do, and which shops they may see.
The `users` table, the auth seam, the permission matrix and territory scoping
live here; channels (who a message arrives from) are slice 2.

```
request ── auth seam ──▶ users row ──▶ Principal{tenant, role, user_id, scope}
                │                                      │
          dev header or                       permission matrix (roles.py)
          Stytch session                              │
                                              404 / 403 with a sentence
```

## The seam: one caller object, two ways in, one switch

`MIMI_AUTH_MODE` decides how a caller is established. The value is either
`dev` or `stytch`, and never both at once: `dev` refuses a Stytch token and
`stytch` refuses the dev header. Every route below `caller()` gets the same
`Principal` whichever way the caller came in, so nothing downstream knows or
cares which one it was.

| Setting | Default | What it does |
| --- | --- | --- |
| `MIMI_AUTH_MODE` | `dev` | `dev`: the caller is named by `X-Actor-Id`. `stytch`: a Stytch session JWT is the only way in. |
| `MIMI_DEV_ACTOR` | `1` | In `dev` mode, `0` turns the `X-Actor-Id` header off. Production refuses it either way. |
| `MIMI_STYTCH_JWKS_URL` | none | Stytch's key set (`https://<project>.stytch.com/v1/b2b/sessions/jwks/<project-id>` or the consumer equivalent). Required for `stytch`. |
| `MIMI_STYTCH_ISSUER` | none | Checked when set (`stytch.com/<project-id>` or the B2B issuer). |
| `MIMI_STYTCH_AUDIENCE` | none | Checked when set. |
| `MIMI_STYTCH_LEEWAY_S` | `60` | Clock leeway for `exp` / `nbf`. |
| `MIMI_STYTCH_JWKS_TTL_S` | `300` | How long a fetched key set is trusted. An unknown `kid` refreshes immediately, so a rotation never waits for this. |

`X-Actor-Id` is a `u_...` id from our own `users` table. It exists for
development, the simulator and the tests; `Settings.check` refuses it whenever
`MIMI_ENV=production`, and refuses to start a production API that is not on
`MIMI_AUTH_MODE=stytch`.

Three kinds of caller come out of the seam:

* **a person** (`source` is `dev-actor` or `stytch`): their `users` row decides
  role and scope;
* **a distributor key** (`source` = `key`): `MIMI_API_KEYS` entries. The
  WhatsApp webhook and the simulator use them. An `owner` key acts as the
  distributor with no person attached (the console before sign-in); a
  `channel` key is `role=channel` with no scope at all.

A token names a subject, never a role. `role: "owner"` in any claim buys
nothing (there is a test). The role is read from the `users` row, every time.

### Refusals name the reason

A 401 says which check failed: the token has expired, the signature does not
match, the issuer is not the configured one, the audience is not this
application, the key is not in the provider's JWKS, the algorithm is not
accepted (`none` and HMAC are refused by name), or the account has not been
invited. An operator should not have to guess between a rotated key and a
wrong clock.

Verification is standard-library RSA (RS256/384/512) over a JWKS fetched with
`httpx`; there is no provider SDK in the request path. The token is a bearer
credential that already arrives as a string, so a small verifier plus one
cached HTTP call is all this needs, and it keeps the dependency surface (and
the upgrade treadmill) off the request path. If Stytch's SDK ever earns its
place it would be for the invite and OTP endpoints of the console, not here.

## The `users` table

One row per person who may act in a distributor's account (`mimi/store/schema_identity.py`).

| Column | Meaning |
| --- | --- |
| `user_id` | `u_...`, opaque, ours. |
| `tenant_id` | The distributor. |
| `external_subject` | Stytch's user id. Null until the person's first sign-in. |
| `email`, `phone_e164` | At least one is present (a check constraint), each nullable, each unique per distributor. Phone is E.164. |
| `name` | How Mimi addresses them. |
| `role` | `owner`, `supervisor`, `rep`. (`brand_viewer` is in the matrix, deferred until brand logins exist.) |
| `status` | `invited`, `active`, `disabled`. |
| `created_at`, `last_seen_at` | When the row was made, when they last signed in (written at most once a minute). |
| `created_by_user_id` | Who added them, the first one excepted. |

Rules the code enforces, with tests:

* **one owner per distributor** (`store.create_user`, `store.set_role`); a
  second owner is a 409 sentence, and the owner cannot be disabled or demoted
  by accident. This is a rule in code, not a unique index on `role`, because a
  partial unique index behaves differently across SQLite and Postgres and the
  message matters more than the constraint;
* at least one of phone or email (the database says so, and the API says so
  with a sentence);
* phone and email are unique per distributor, compared on normalised values
  (E.164 digits for phone, casefolded for email), so `0803…` and `+234803…` are
  one person;
* nobody can invite as owner except the founder path (`store.ensure_owner`),
  which is idempotent and used to bootstrap a distributor;
* **an agent is never a `users` row.** People are people; agents act through
  the service keys.

## Getting in: the invite, and no just-in-time accounts

The owner adds a person in the console (`POST /v1/users`). That row *is* the
invite: role, name, and the phone or email the person will sign in with. Nothing
is emailed by this code base; the person signs in at the console (Stytch sends
the OTP or the magic link), and the session comes to the API.

* **First successful sign-in links the row.** If the `users` row has no
  `external_subject`, the verified Stytch id is written to it. Matching is by
  Stytch subject first, then by the verified email or phone the session carries
  (a project that puts either in `custom_claims` or on the token links without
  any extra console step).
* **A verified phone or email with no row is "no account"**: a 401 saying *this
  account has not been invited: ask the distributor's owner to add you*, and
  nothing is created. If a verified login could create a row, an uninvited
  number would be a way into a distributor's book.
* **Already linked to a different Stytch id is refused** with a 403 sentence,
  and the row is left alone. This is the account-takeover guard: someone who
  gains control of a phone number must not be able to take over an account that
  already has its own sign-in. Fixing it is the owner's job (reissue the invite
  after disabling the old row), not the sign-in step's.
* **Disabled is 403**, invited is 403 ("not activated yet"), so a person who is
  in the book but not yet in use learns why rather than seeing an empty console.

## Roles and what each may do

One matrix in `mimi/identity/roles.py`, one function per action, no scattered
`if role ==` checks. The test walks every (role, action) pair, refusals
included, so a new role or a new action cannot slip through untested.

| Action | owner | supervisor | rep | brand_viewer |
| --- | --- | --- | --- | --- |
| `view_channel` | every | their territory | their beat | never |
| `close_item` (money) | every | their reps | never | never |
| `close_item` (not money) | every | their territory | their beat | never |
| `approve_money` | every | their reps | never | never |
| `change_credit_limit` | every | never | never | never |
| `manage_members` | every | never | never | never |
| `manage_settings` | every | never | never | never |
| `undo_write` | every | never | never | never |

The rule kinds are `every`, `scope`, `own_rep_money`, `own_non_money` and
`never`; an action is a named function (`can_close_item`, `can_approve_money`,
`can_change_credit_limit`, `can_manage_members`, …) so a caller reads as a
sentence. Money means a money word in the tool or intent kind
(`roles.is_money`); a credit-limit action is `roles.is_credit_limit`. A
supervisor deciding money for their own rep is the one place a non-owner says
yes to a payment, and the approval row records *who* decided and on what.

## Ground: territories and beats

A supervisor covers areas; a rep covers a beat. The book already knew reps
(`reps`, `shops.rep_id`), so no new territory model was invented:

* `user_territories (tenant_id, user_id, area, added_at)` says which
  `shops.area` values a supervisor covers. Areas are **normalised on write and
  on comparison** (NFKC, trimmed, casefolded, inner whitespace collapsed), so
  `"  Oke-Ila "` and `"oke-ila"` are one territory. Indexed by
  `(tenant_id, area)` as well as by user: slice 2 turns this into the
  membership list for channels.
* A rep's ground is the `reps` row whose phone matches their `users` row; the
  shops are those with that `rep_id`. A rep with **no matching `reps` row gets
  an empty scope**, not every shop and not an error.
* The shop's own ground is `shops.area` and `shops.rep_id`.

**A finding from the seeded world, worth knowing before anyone trusts
`reps.areas`:** the areas listed on a rep row disagree with the shops that rep
actually serves (rep one lists Basiri and Ajilosun but serves Odo-Ado, Oke-Ila,
Oja Oba, Okesa and Adebayo), and both reps serve shops in both supervisor
territories. Coverage therefore comes from `shops.area` (supervisor) and
`shops.rep_id` (rep); `reps.areas` only contributes to "which reps are mine".
The synthetic world should be tidied so the two agree before anything is drawn
from `reps.areas`.

`GET /v1/me` says what the caller's ground is:

```json
{"user": {"user_id": "u_…", "role": "supervisor", "name": "…"},
 "role": "supervisor", "source": "stytch",
 "scopes": {"all": false, "empty": false, "cover_by": "area",
            "areas": ["ajilosun", "basiri"], "rep_ids": ["r_…", "r_…"]},
 "channels": []}
```

`cover_by` is the one axis that decides shops (`all`, `area`, `rep`), so a
supervisor's areas and reps are never ORed together by accident. Areas are the
keys the console matches on (`shops.area`); showing "Oke-Ila" is the console's
job.

**Empty ground is not "no data yet".** A supervisor with no territories and a
rep with no `reps` row both answer `scopes.empty: true`. An empty book answers
`scopes.empty: false` with a scope, and `GET /v1/shops` `total` of `0`. The
console shows "no territory assigned yet, ask the owner" for the first and
"this distributor has no shops yet" for the second, so nobody is sent to chase
data that is simply not theirs to see.

### 404, not 403, outside the ground

An id outside the caller's ground answers exactly what an unknown id answers:
`404 {"detail": "not found"}`. A 403 would confirm the shop exists, which is
already information about a distributor's book that a rep in another territory
should not have. The refusal *inside* the ground but above the caller's role is
a 403 with a plain sentence ("a credit limit is the owner's call"). There is a
table-driven test over every route that names a shop.

## Setting this up

What the owner (Atlasflow) creates, and what goes where. No secret is ever read
from a file in this repository: every value below comes from the environment
(or from the platform's secret store, injected as the environment).

**Stytch** (the sign-in provider):

1. A project, per environment. Note the **project id** and the environment
   (`test` or `live`); they are in the URLs.
2. Login methods: **phone (SMS OTP)** and **email (magic link or OTP)** both
   enabled, and Google OAuth for staff if wanted. Phone is the primary way in
   for reps and, later, shop owners; email and Google are for internal staff. A
   person may have a phone only, an email only, or both.
3. **Session JWT** enabled, with the session lifetime you want (a short one:
   the API verifies it, it is not a refresh token). Note the **JWKS URL** and
   the **issuer** and **audience** values. If the console should be able to link
   an invite without the person typing the same email again, put the verified
   email or `phone_number` in **custom claims**; otherwise the invite carries
   the Stytch user id the console created (both paths are supported).
4. SMS for Nigerian numbers goes through **Termii**, configured inside Stytch's
   SMS settings (see below), not called by this code.

Into the environment / Atlasflow secret store:

```
MIMI_AUTH_MODE=stytch
MIMI_STYTCH_JWKS_URL=https://<project>.stytch.com/v1/b2b/sessions/jwks/<project-id>
MIMI_STYTCH_ISSUER=stytch.com/<project-id>
MIMI_STYTCH_AUDIENCE=<the audience the project issues>
MIMI_STYTCH_JWKS_TTL_S=300
```

**Termii** (SMS delivery for Nigerian numbers):

1. An account with sender id approval for the distributor-branded sender (Nigerian
   carriers require the sender id to be registered; allow a few days).
2. Termii's API key and the sender id go into **Stytch's custom SMS provider
   settings**, not into this repository and not into this API. Stytch calls
   Termii; Mimi never holds the Termii key.

Rotating a Stytch key needs no deploy: the API refetches the key set when it
sees an unknown `kid`, and at most `MIMI_STYTCH_JWKS_TTL_S` behind otherwise.

## Tests

`tests/test_identity.py` and `tests/test_api_identity.py` cover the table above,
the seam (both ways, and that neither leaks into the other), linking, the
takeover guard, no-just-in-time provisioning, normalised territories, and a
table over every scoped route. The token tests sign with a **generated local
RSA key** and serve a JWKS built from it (`tests/test_identity.py:_TestKey`), so
no real key, project id or phone number is in the repository or in any fixture.

## What this slice does not do

* Channels (who a WhatsApp or dashboard message came from) are slice 2;
  `GET /v1/me` reports `channels: []` until then.
* `brand_viewer` is in the matrix and refused everywhere; brand logins come
  later.
* There is no password, no session store and no refresh: the API verifies a
  session it is handed and keeps nothing.

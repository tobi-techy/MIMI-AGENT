"""Per-tenant model spend: a ceiling the runtime enforces, not a dashboard.

The ``spend`` table records what a distributor's model calls cost. This module
turns that record into a limit that holds while the bill is running:

* **A bounded default.** The cap is N250 a day per active shop, floored at one
  shop (``mimi.observe.allowance`` computes the shop count at Lagos midnight;
  this module owns the arithmetic and the words). A caller that forgot to pass
  a cap, or a negative one, gets that floor, never "unlimited".
* **One comparison.** ``decide`` is the only place "is this tenant over its
  day?" is answered: over when ``spent + reserve >= cap`` (the reserve is what
  the next call could cost, when the caller knows it).
* **Refuse before the call, not after.** The metering seam
  (``mimi.models.metering``) asks ``admit`` before every paid call and raises
  ``BudgetExhausted`` when the day is spent, so the expensive path stops
  instead of running up a bill across workers. Code paths make no paid call,
  so rules, book reads and plain-code replies keep working for a tenant that
  is out of allowance: the cheap, certain path survives.
* **One plain sentence.** ``question`` is what the owner reads (the figures,
  turns first), ``summary`` is the one line the log and the alert carry
  (tenant figures, no shop data) and ``refusal`` is what a person gets when
  the model work cannot run today. Turns are the unit the owner sees; money is
  the limit enforced.

Money is integer kobo. Nothing here writes to the spend table; it only reads
what ``mimi.models.metering.book`` recorded. Pure and total: no imports from
the store, the harness or the models, so every layer can use it.
"""

from __future__ import annotations

from dataclasses import dataclass

# The owner's decision (30 Oct 2026): N250 a day per active shop, floored at
# one shop, so the floor is also the smallest cap this code can produce.
SHOP_ALLOWANCE_KOBO = 25_000
# The one-shop floor: no activity still gets N250, never zero.
DEFAULT_DAILY_CAP_KOBO = SHOP_ALLOWANCE_KOBO
# What one owner "raise it" approval adds at least (the loop's existing offer).
MIN_RAISE_KOBO = 50_000
# N16.42 a model turn, measured from recorded tokens in ``COST.md`` (the
# "after, this branch, defaults" row: N705.93 for 43 agent turns). This is a
# measured average, not a constant: it is what turns a money cap into the
# "N of M model turns" a founder reads, and money is what is enforced.
AVG_TURN_KOBO = 1_642


def shop_allowance(shops: int) -> int:
    """N250 a day per active shop, with a one-shop floor."""
    return SHOP_ALLOWANCE_KOBO * max(1, int(shops))


def turns_for(kobo: int) -> int:
    """A money cap in model turns, at the measured average."""
    return max(0, int(kobo)) // AVG_TURN_KOBO


def turns_used_kobo(turns: int) -> int:
    """The money a turn count means, for a report that has to show both."""
    return max(0, int(turns)) * AVG_TURN_KOBO


class BudgetExhausted(RuntimeError):
    """The distributor's day's model allowance is spent: refuse the paid call.

    Raised at the metering seam before the call is made, never after. It
    carries the figures for the log and the trace, and never for a person:
    a shop or owner reads ``refusal`` or the loop's own question instead.
    """

    def __init__(self, *, tenant_id: str = "", role: str = "", spent_kobo: int = 0,
                 cap_kobo: int = DEFAULT_DAILY_CAP_KOBO, day: str = "") -> None:
        self.tenant_id = tenant_id
        self.role = role
        self.spent_kobo = int(spent_kobo)
        self.cap_kobo = int(cap_kobo)
        self.day = day
        super().__init__(
            f"model allowance spent for {tenant_id or 'this distributor'} "
            f"({self.spent_kobo} of {self.cap_kobo} kobo, {role or 'model'} call)"
        )


@dataclass(frozen=True)
class Decision:
    """Whether the next paid call may run, and the figures behind it."""

    allowed: bool
    spent_kobo: int
    cap_kobo: int
    reserve_kobo: int = 0

    @property
    def over(self) -> bool:
        return not self.allowed


def cap_or_default(cap_kobo: int | None) -> int:
    """A cap that is a number. A missing or negative one is the one-shop floor."""
    if cap_kobo is None or int(cap_kobo) < 0:
        return DEFAULT_DAILY_CAP_KOBO
    return int(cap_kobo)


def decide(spent_kobo: int, cap_kobo: int | None, *, reserve_kobo: int = 0) -> Decision:
    """The one comparison: is this distributor's day spent?"""
    cap = cap_or_default(cap_kobo)
    spent = max(0, int(spent_kobo))
    reserve = max(0, int(reserve_kobo))
    return Decision(spent + reserve < cap, spent, cap, reserve)


def admit(*, tenant_id: str, role: str, spent_kobo: int, cap_kobo: int | None,
          day: str = "", reserve_kobo: int = 0) -> None:
    """Raise ``BudgetExhausted`` when the day is spent; return when it is not."""
    d = decide(spent_kobo, cap_kobo, reserve_kobo=reserve_kobo)
    if not d.allowed:
        raise BudgetExhausted(tenant_id=tenant_id, role=role, spent_kobo=d.spent_kobo,
                              cap_kobo=d.cap_kobo, day=day)


def naira(kobo: int) -> str:
    """N500 for 50,000 kobo, with no decimals when the amount is whole naira."""
    whole, part = divmod(max(0, int(kobo)), 100)
    return f"N{whole:,}" if not part else f"N{whole:,}.{part:02d}"


def question(spent_kobo: int, cap_kobo: int | None) -> str:
    """What Mimi asks the owner once a day when the allowance runs out.

    Turns first, money behind them: a turn count is what a founder can reason
    about, and the naira is the limit code enforces. The figures are for the
    owner's card, which code writes; a shop reads ``refusal``.
    """
    cap = cap_or_default(cap_kobo)
    return (f"Mimi has used {turns_for(spent_kobo)} of today's {turns_for(cap)} model "
            f"turns ({naira(spent_kobo)} of {naira(cap)}), so helpers are paused until "
            "tomorrow. Raise today's allowance?")


def tenant_name(tenant_id: str) -> str:
    """A distributor's readable name from its id: ``coral-lane`` -> Coral Lane."""
    words = str(tenant_id or "").replace("_", " ").replace("-", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words)


def refusal(*, tenant: str = "", shops: int = 0, cap_kobo: int | None = None,
            lang: str = "en") -> str:
    """The plain sentence a person gets while the model work waits.

    It names the distributor, how many shops were counted, the day's cap and
    what stopped: replies that need the model. Plain words, no internal ids,
    no step counts and no model text (docs/MIMI-VOICE.md). One sentence for
    one cause, whether the loop or the metering seam stopped the call.
    """
    name = tenant_name(tenant)
    who = f"{name}'s Mimi" if name else "Mimi"
    cap = cap_or_default(cap_kobo)
    if lang == "pcm":
        shops_part = f"{shops or 1} active shops"
        return (f"Thank you, I don see your message. {who} don use today's model "
                f"allowance for {shops_part} ({naira(cap)}), so replies wey need "
                "the model go wait. Nothing lost: your message dey next when the "
                "allowance come back.")
    shops_part = f"{shops or 1} active shop" + ("" if (shops or 1) == 1 else "s")
    return (f"Thanks, I have your message. {who} has used today's model allowance "
            f"for {shops_part} ({naira(cap)}), so replies that need the model wait. "
            "Nothing is lost: your message is next when the allowance is back.")


def summary(spent_kobo: int, cap_kobo: int | None) -> str:
    """One line for a log or an alert: tenant figures, no shop data."""
    cap = cap_or_default(cap_kobo)
    return (f"{turns_for(spent_kobo)} of {turns_for(cap)} model turns used "
            f"({naira(spent_kobo)} of {naira(cap)})")

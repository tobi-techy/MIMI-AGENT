"""Per-job budgets: steps, working seconds and naira of model spend.

Money here is integer kobo (``mimi.types.Kobo``). A budget is a ceiling the
runner checks before every step; hitting any one parks the job as
``waiting_on_owner`` with a plain question, never a silent stop.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Budget:
    max_steps: int = 12
    max_seconds: float = 60.0
    # N50 of model spend by default (RAI-278).
    max_kobo: int = 5_000

    def extended(self, other: Budget) -> Budget:
        """The owner said keep going: add their grant on top."""
        return Budget(
            max_steps=self.max_steps + other.max_steps,
            max_seconds=self.max_seconds + other.max_seconds,
            max_kobo=self.max_kobo + other.max_kobo,
        )

    def to_dict(self) -> dict[str, float | int]:
        return {
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_kobo": self.max_kobo,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, float | int]) -> Budget:
        return cls(
            max_steps=int(raw["max_steps"]),
            max_seconds=float(raw["max_seconds"]),
            max_kobo=int(raw["max_kobo"]),
        )


@dataclass
class Spend:
    steps: int = 0
    seconds: float = 0.0
    kobo: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return {"steps": self.steps, "seconds": self.seconds, "kobo": self.kobo}

    @classmethod
    def from_dict(cls, raw: dict[str, float | int]) -> Spend:
        return cls(
            steps=int(raw["steps"]),
            seconds=float(raw["seconds"]),
            kobo=int(raw["kobo"]),
        )


@dataclass(frozen=True)
class BudgetHit:
    dimension: str
    limit: float | int
    spent: float | int

    def question(self) -> str:
        """What Mimi asks the owner, in plain words (30 Oct 2026 run 2): no
        step, second or naira budget figures; those stay on the hit itself
        (``dimension``, ``limit``, ``spent``) for the logs."""
        return "This one's taking more work than usual. Nothing was lost. Keep going?"


def first_hit(budget: Budget, spend: Spend) -> BudgetHit | None:
    """The first exhausted dimension, checked before a step starts."""
    if spend.steps >= budget.max_steps:
        return BudgetHit("steps", budget.max_steps, spend.steps)
    if spend.seconds >= budget.max_seconds:
        return BudgetHit("seconds", budget.max_seconds, spend.seconds)
    if spend.kobo >= budget.max_kobo:
        return BudgetHit("kobo", budget.max_kobo, spend.kobo)
    return None

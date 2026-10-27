"""Proof eval as a gated Langfuse experiment, for ``langfuse/experiment-action``.

Used only by the optional ``.github/workflows/langfuse-experiment.yml``
(off by default). The task replays the recorded Muse Spark answers, so the
run needs Langfuse keys but no model key and costs no model calls.

Gates (``RegressionError`` fails the job): no fake may end likely_genuine,
and verdict accuracy must stay at or above 95%.
"""

from __future__ import annotations

from typing import Any

from langfuse import RegressionError, RunnerContext

from mimi.observe.experiments import EVALUATORS, _Inline, _proof_task, _replay_model, run_rates
from mimi.observe.langfuse_export import LangfuseSettings


def experiment(context: RunnerContext) -> Any:
    settings = LangfuseSettings.from_env(environment="eval") or LangfuseSettings(
        public_key="", secret_key="")
    model = _replay_model("proof")
    client = context.client
    result = context.run_experiment(
        name="proof-eval",
        description="Proof eval (replayed Muse Spark answers) gated in CI",
        task=_proof_task(model, _Inline(settings, client)),
        evaluators=EVALUATORS["proof"],
        run_evaluators=[run_rates],
        max_concurrency=4,
    )
    rates = {e.name: e.value for e in result.run_evaluations}
    if (rates.get("false_likely_genuine_rate") or 0) > 0:
        raise RegressionError(result=result, metric="false_likely_genuine_rate",
                              value=rates["false_likely_genuine_rate"], threshold=0.0)
    if (rates.get("verdict_accuracy") or 0) < 0.95:
        raise RegressionError(result=result, metric="verdict_accuracy",
                              value=rates.get("verdict_accuracy"), threshold=0.95)
    return result

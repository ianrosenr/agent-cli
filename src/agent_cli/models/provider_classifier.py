"""A ``SignalClassifier`` backed by one provider model, through ``ProviderAdapter.invoke``.

One call per batch. The request carries only the compact classifier inputs and the signal
vocabulary: no skill bodies, no chat history. Output is parsed strictly; anything malformed
raises, and the V1-06 seam records the failure and falls back to deterministic routing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from agent_cli.adapters import ProviderAdapter, ProviderRequest
from agent_cli.models.access import ModelPath
from agent_cli.skills.classifier import ClassificationInput, ClassificationResult
from agent_cli.skills.policy import SignalVocabulary
from agent_cli.skills.tokens import DEFAULT_ESTIMATOR, TokenEstimator

PROMPT_VERSION = "1"
"""Bump when the prompt or output contract changes; it is part of the classifier identity."""
MAX_SIGNALS_PER_TASK = 8
MAX_EVIDENCE_CHARS = 200
OUTPUT_TOKENS_BASE = 64
OUTPUT_TOKENS_PER_TASK = 96

SYSTEM = """\
You label software tasks with routing signals. For each task, choose zero or more signal IDs \
from the given vocabulary that the task clearly involves. Use only IDs from the vocabulary. \
Reply with JSON only, no prose, in exactly this shape:
{"results": [{"task_id": "<id>", "signals": ["<signal>", ...], "evidence": "<short reason>"}]}"""


class ClassifierOutputError(ValueError):
    """The model's reply did not follow the output contract."""


class ClassifierBatchTooLarge(ValueError):
    """The batch would not fit the model's context limit, so no call was made."""


class ProviderSignalClassifier:
    def __init__(
        self,
        adapter: ProviderAdapter,
        path: ModelPath,
        selection: str,
        estimator: TokenEstimator = DEFAULT_ESTIMATOR,
    ) -> None:
        self.adapter = adapter
        self.path = path
        self.selection = selection
        """How the model was chosen (``auto`` or ``selected``), recorded in the identity."""
        self.estimator = estimator
        """Conservative token estimate, used to keep a batch within the context limit."""

    @property
    def identity(self) -> str:
        """``name@version``, then the model, provider, and access path for telemetry."""
        return (
            f"provider-classifier@{PROMPT_VERSION} ({self.selection}: {self.path.model_id} via "
            f"{self.path.describe()})"
        )

    def classify(
        self, inputs: Sequence[ClassificationInput], vocabulary: SignalVocabulary
    ) -> Sequence[ClassificationResult]:
        if not inputs:
            return []
        prompt = render_prompt(inputs, vocabulary)
        max_output = OUTPUT_TOKENS_BASE + OUTPUT_TOKENS_PER_TASK * len(inputs)
        limit = self.path.context_limit
        if limit is not None:
            needed = self.estimator.estimate(SYSTEM + prompt) + max_output
            if needed > limit:
                # Raised before any call; the seam falls back to deterministic routing.
                raise ClassifierBatchTooLarge(
                    f"batch of {len(inputs)} needs about {needed} tokens; "
                    f"{self.path.model_id} via {self.path.provider.id} allows {limit}"
                )
        response = self.adapter.invoke(
            ProviderRequest(
                model_id=self.path.model_id,
                prompt=prompt,
                system=SYSTEM,
                max_output_tokens=max_output,
            )
        )
        return parse_reply(response.text, {item.task_id for item in inputs})


def render_prompt(inputs: Sequence[ClassificationInput], vocabulary: SignalVocabulary) -> str:
    payload = {
        "vocabulary": sorted(vocabulary.signals),
        "tasks": [
            {
                "task_id": item.task_id,
                "role": item.role,
                "statement": item.statement,
                "spec_summary": item.spec_summary,
                "paths": list(item.paths),
                "known_signals": list(item.known_signals),
            }
            for item in inputs
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=None, separators=(",", ":"))


def parse_reply(text: str, task_ids: set[str]) -> list[ClassificationResult]:
    """Validate and normalize the reply. Vocabulary filtering is left to the V1-06 seam, which
    records rejected IDs; this only enforces shape and bounds."""
    try:
        data: Any = json.loads(strip_fence(text))
    except json.JSONDecodeError as error:
        raise ClassifierOutputError(f"classifier reply is not JSON: {error}") from error
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ClassifierOutputError("classifier reply must be an object with a results list")

    parsed: list[ClassificationResult] = []
    seen: set[str] = set()
    for index, item in enumerate(results):
        if not isinstance(item, dict) or not isinstance(item.get("task_id"), str):
            raise ClassifierOutputError(f"results[{index}] must be an object with a task_id")
        task_id = item["task_id"]
        if task_id not in task_ids:
            continue
        if task_id in seen:
            raise ClassifierOutputError(f"duplicate result for task_id {task_id!r}")
        seen.add(task_id)
        signals = item.get("signals", [])
        if not isinstance(signals, list) or not all(isinstance(s, str) for s in signals):
            raise ClassifierOutputError(f"results[{index}].signals must be a list of strings")
        evidence = item.get("evidence", "")
        if not isinstance(evidence, str):
            raise ClassifierOutputError(f"results[{index}].evidence must be a string")
        normalized = tuple(dict.fromkeys(s.strip() for s in signals if s.strip()))
        parsed.append(
            ClassificationResult(
                task_id,
                normalized[:MAX_SIGNALS_PER_TASK],
                evidence.strip()[:MAX_EVIDENCE_CHARS],
            )
        )
    return parsed


def strip_fence(text: str) -> str:
    """Tolerate a reply wrapped in a single Markdown code fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.removesuffix("```").strip()
    return stripped

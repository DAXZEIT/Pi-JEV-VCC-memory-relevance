"""Convert backend logits into classifier answers and JSON-compatible responses.

This module owns input-label alignment, v1 softmax/scoring, diagnostic filtering,
and the response envelope. It does not run the model or calculate token usage.
Use build_answers() for answers alone or build_response() for model/answers/usage.

Accepted logits are keyed by the plan's branch IDs, with each value being:

1. A mapping of output-label strings to floats or scalar torch tensors.
2. A 1-D floating torch tensor already ordered like question.output_labels.
3. A 1-D vocabulary-logit torch tensor plus an explicit per-branch token mapping.

Torch is optional and imported only when a tensor value is supplied. Tensor
inputs may be on CPU or an accelerator and may require gradients: we detach them
and transfer only selected logits to CPU float32 for scoring. Return values are
ordinary Python dictionaries, lists, and numbers suitable for JSON serialization.

Example using floats::

    from common import prepare_prompt, build_response

    plan = prepare_prompt({
        "model": "my-model", "state": "The bicycle is red.",
        "questions": {"color": {
            "type": "choice", "instructions": "Color?",
            "criteria": {"red": None, "blue": None},
        }},
    })
    response = build_response(plan, {"0": {"A": 3.0, "B": 1.0}}, input_tokens=123)
    assert response["answers"]["color"]["choice"] == "red"

Example with vocabulary logits (illustrative token IDs)::

    import torch

    vocab_logits = torch.zeros(100)
    vocab_logits[42], vocab_logits[73] = 3.0, 1.0
    response = build_response(
        plan, {"0": vocab_logits}, input_tokens=123,
        token_ids={"0": {"A": 42, "B": 73}},
    )
    assert response["answers"]["color"]["choice"] == "red"

The adapter must verify token IDs against the rendered answer boundary. IDs in
this example are placeholders, not universal IDs for A/B. Pass one scored
position per branch, not the full [batch, sequence, vocabulary] model output.
Selection of that position and mapping batch rows back to branch IDs belong to
the adapter. A mapped vocabulary ID is used as an index, never guessed by decoding.

v1 normalizes only allowed labels. Choice selects a candidate; score averages
zero-based rubric indices; Noul maps the mean of bins 1..9 to [0.01, 0.99].
Confidence is the largest label probability, not a calibrated correctness estimate
or confidence interval. Noul has no separate confidence field. Future versions
that change scoring rules must retain and dispatch to the correct old mapping.
"""

from collections.abc import Mapping

import numpy as np

from .prompt_builder import PromptPlan

# Only these fields are exposed in the default answers. Numerical scoring also
# computes diagnostics (e.g. variance, ties, margins) which stay internal unless
# advanced=True. Different question types supply different subsets of this set.
PUBLIC_FIELDS = {
    "type",
    "choice",
    "score",
    "noul",
    "confidence",
    "probabilities",
    "legend",
}


def build_answers(
    plan: PromptPlan,
    logits: Mapping,
    *,
    token_ids: Mapping | None = None,
    advanced=False,
):
    """Logits are keyed by branch_id, then exact output label (case-sensitive).

    Each branch accepts a label-to-scalar mapping, a one-dimensional tensor in
    output_labels order, or a vocabulary tensor with token_ids supplied. For the
    latter, token_ids maps branch IDs to {output_label: vocabulary_token_id}.
    All and only planned branches/labels must be supplied; map order is irrelevant.
    Tensors are detached, selected on their device, then converted to CPU float32.
    This API performs inference postprocessing, not differentiable training.
    Values must be finite raw logits, not probabilities or sampled token strings.
    The scorer validates row shape and finiteness after this function aligns rows.

    Returns answers keyed by the original question IDs, in plan order. Missing or
    extra branches/labels raise ValueError instead of returning partial answers.

    advanced=False exposes only PUBLIC_FIELDS. advanced=True retains scoring
    diagnostics. Raw logits require BOTH advanced=True and the original request's
    options.raw_logits=True. Enabling diagnostics does not change the scores.
    """
    # Require a complete result for exactly this plan. Extra results can indicate
    # that a backend accidentally mixed requests or used the wrong prompt plan.
    if set(logits) != {q.branch_id for q in plan.questions}:
        raise ValueError("Logits must contain exactly the planned branch IDs")
    # Convert unordered backend maps into ordered numeric rows. Each row must
    # follow output_labels so scoring can pair it with the public answer_labels.
    if token_ids is not None and set(token_ids) != {
        q.branch_id for q in plan.questions
    }:
        raise ValueError("Token mappings must contain exactly the planned branch IDs")
    rows = [
        _logit_row(
            question,
            logits[question.branch_id],
            None if token_ids is None else token_ids[question.branch_id],
        )
        for question in plan.questions
    ]
    answers = _assemble(plan._request, plan.questions, rows)
    if not advanced:
        answers = {
            key: {k: v for k, v in answer.items() if k in PUBLIC_FIELDS}
            for key, answer in answers.items()
        }
    return answers


def build_response(
    plan: PromptPlan,
    logits: Mapping,
    *,
    input_tokens: int,
    token_ids: Mapping | None = None,
    output_tokens: int = 0,
    advanced=False,
):
    """Wrap scored answers with the model identifier and caller-supplied usage.

    token_ids has the same meaning as in build_answers().

    input_tokens is required because only the adapter knows the rendered prompt
    and its accounting policy. output_tokens defaults to zero for logits-only
    inference. Both must be nonnegative Python integers (booleans are rejected).

    advanced=True adds version/calibration metadata and preserves diagnostic
    answer fields. This function does not add HTTP status codes, backend timing,
    persistent-cache billing, or a generated completion string.
    """
    for name, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
    ):
        # bool subclasses int in Python; an exact type check excludes it.
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    response = {
        "model": plan._request.model,
        "answers": build_answers(plan, logits, token_ids=token_ids, advanced=advanced),
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    # Metadata describes the plan that produced the logits, not a version
    # inferred from the shape or values of the backend's result.
    if advanced:
        response["metadata"] = {
            "template_version": plan.template_version,
            "calibration": "not_calibrated",
        }
    return response


def _tensor(value):
    """Load the optional tensor dependency only for non-numeric inputs."""
    try:
        import torch
    except ImportError as exc:
        raise ValueError("Tensor logits require PyTorch") from exc
    if not isinstance(value, torch.Tensor) or not value.is_floating_point():
        raise ValueError("Expected a floating-point torch tensor")
    return value


def _scalar(value):
    """Allow float scalars or 0-D floating tensors in label-keyed maps."""
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(
        value, bool
    ):
        return float(value)
    value = _tensor(value)
    if value.ndim != 0:
        raise ValueError("Label logits must be scalars")
    return value.detach().float().cpu().item()


def _logit_row(question, values, token_ids):
    """Align a branch's labels without transferring the entire vocabulary."""
    labels = question.output_labels
    if isinstance(values, Mapping):
        if token_ids is not None:
            raise ValueError("Token mappings apply only to vocabulary tensors")
        if set(values) != set(labels):
            raise ValueError(
                f"Unexpected or missing output labels for branch {question.branch_id!r}"
            )
        return [_scalar(values[label]) for label in labels]

    values = _tensor(values)
    if values.ndim != 1:
        raise ValueError("Expected a one-dimensional logit tensor per branch")
    if token_ids is None:
        if values.shape[0] != len(labels):
            raise ValueError(
                "Expected one logit per output label; supply token_ids for vocabulary tensors"
            )
        selected = values.detach()
    else:
        if not isinstance(token_ids, Mapping) or set(token_ids) != set(labels):
            raise ValueError("Token mapping must contain exactly the output labels")
        ids = [token_ids[label] for label in labels]
        if any(type(i) is not int or not 0 <= i < values.shape[0] for i in ids):
            raise ValueError("Token IDs must be integers within the vocabulary tensor")
        if len(set(ids)) != len(ids):
            raise ValueError("Output labels must map to distinct token IDs")
        # Index first on the source device. Only the handful of permitted label
        # logits are converted/transferred; unrelated vocabulary logits are unused.
        selected = values.detach()[ids]
    return selected.float().cpu().tolist()


def _assemble(request, branches, logits):
    """Return diagnostic-rich answers keyed by original question ID.

    request is the validated snapshot belonging to the prompt plan. branches
    are its ordered ScoringQuestion entries. logits is an equally sized sequence
    of numeric rows, each ordered exactly like its branch's output_labels.

    This is an internal boundary: build_answers() has already verified branch and
    label identity. Here we check row counts, row lengths, and finite values.
    Conversion and arithmetic use NumPy float32 to retain the reference behavior.
    Returned scalars/lists are converted to ordinary JSON-compatible values.
    """
    if len(branches) != len(logits):
        raise ValueError("Expected one logit row per question")
    answers = {}
    for branch, values in zip(branches, logits):
        question = request.questions[branch.question_id]
        # output_labels and answer_labels are parallel tuples. The adapter has
        # already ordered the row by output_labels; use public labels from here.
        labels = branch.answer_labels
        z = np.asarray(values, dtype=np.float32)
        if z.shape != (len(labels),) or not np.isfinite(z).all():
            raise ValueError("Expected one finite logit per output label")
        # Subtracting the maximum stabilizes softmax without changing its result.
        # No temperature or other completion setting participates in v1 scoring.
        p = np.exp(z - z.max())
        p /= p.sum()
        if question.type == "noul":
            # Noul always uses these nine integer bins, even though the prompt
            # describes probability endpoints 0.1 and 0.9.
            bins = np.arange(1, 10, dtype=np.float32)
            mean = float(p @ bins)
            # Variance is in integer-rating units; entropy uses natural logs
            # (nats). Exclude zero probabilities to avoid log(0).
            rating = {
                "bins": bins.tolist(),
                "probabilities": p.tolist(),
                "expected_score": mean,
                "variance": float(p @ ((bins - mean) ** 2)),
                "entropy": float(-(p[p > 0] * np.log(p[p > 0])).sum()),
            }
            if request.options.raw_logits:
                rating["logits"] = z.tolist()
            # Map endpoint ratings to 0.01/0.99, with rating 5 at 0.5.
            # Clipping protects the public range against numerical roundoff.
            answer = {
                "type": "noul",
                "noul": float(
                    np.clip(0.01 + (mean / 10 - 0.1) * (0.98 / 0.8), 0.01, 0.99)
                ),
                "calibrated": False,
                "rating": rating,
            }
        else:
            # argmax selects the first exact tie, preserving input label order.
            winner = int(np.argmax(z))
            answer = {
                "type": question.type,
                "confidence": float(p[winner]),
                "probabilities": dict(zip(labels, p.tolist())),
                "calibrated": False,
            }
            if request.options.raw_logits:
                answer["logits"] = dict(zip(labels, z.tolist()))
            if question.type == "choice":
                # Margin is the gap between the largest two probabilities.
                # Ties are exact equality of the float32 logits, not a tolerance.
                ordered = np.sort(p)
                answer.update(
                    {
                        "choice": labels[winner],
                        "margin": float(ordered[-1] - ordered[-2]),
                        "ties": [
                            label
                            for label, value in zip(labels, z)
                            if value == z[winner]
                        ],
                        "scoring": "direct_label_logits",
                    }
                )
            else:
                # Score returns the distribution's mean, not its winning level.
                # Letter labels for rubrics above ten levels still map to these
                # zero-based indices. The legend retains the original criteria.
                bins = np.arange(len(labels), dtype=np.float32)
                mean = float(p @ bins)
                answer.update(
                    {
                        "score": mean,
                        "legend": {
                            str(i): value for i, value in enumerate(question.criteria)
                        },
                        "variance": float(p @ ((bins - mean) ** 2)),
                        "scoring": "direct_level_logits",
                        "score_mapping": "label_to_zero_based_level",
                    }
                )
        # Public answers use caller-provided question IDs, not inference IDs.
        answers[branch.question_id] = answer
    return answers

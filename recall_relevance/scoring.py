"""Bounded, local-only recall scoring; no extraction, persistence or CLI.

Rows retain input order (including unscored rows). Only caller-owned IDs/refs,
fixed diagnostics, hashes and numeric server metadata leave this module; neither
source text, rendered prompts, server completions nor exception messages do.
Dependencies are those of the existing jev/jev script (numpy/jinja2/pydantic).
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.machinery
import importlib.util
import ipaddress
import json
import math
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import urlsplit


QUESTION = "Would consulting this earlier conversation block via recall help continue the current task correctly from the current context?"
QUESTION_VERSION = "recall-help-v1"
RUBRIC_VERSION = "recall-help-binary-v1"
CRITERIA = {
    "false": "Consulting this earlier block would not help continue the current task correctly. Mere topical similarity is insufficient.",
    "true": "Consulting this earlier block would help continue the current task correctly, for example by recovering a relevant constraint, correction, rejected approach and its reason, decision rationale, or evidence. Helpfulness is not a judgment of truth or current validity; consult the source before relying on it.",
}
_ROOT = Path(__file__).resolve().parents[1]
_LOAD_LOCK = threading.Lock()
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_COMPLETION = {"max_tokens": 1, "temperature": 0.0, "logprobs": 20, "cache_prompt": True}
_USAGE_SCHEMA = {
    "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
    "prompt_tokens_details": {"cached_tokens": None, "audio_tokens": None},
    "completion_tokens_details": {"reasoning_tokens": None, "audio_tokens": None,
                                  "accepted_prediction_tokens": None, "rejected_prediction_tokens": None},
}
_CACHE_SCHEMA = {k: None for k in ("hit", "tokens_reused", "cached_tokens", "tokens_cached",
                                   "tokens_evaluated", "cache_n", "prompt_n")}
_METADATA_SCHEMA = {
    "usage": _USAGE_SCHEMA, "cache": _CACHE_SCHEMA, "cache_usage": _CACHE_SCHEMA,
    "timings": {k: None for k in ("cache_n", "prompt_n", "prompt_ms", "prompt_per_token_ms",
                                  "prompt_per_second", "predicted_n", "predicted_ms",
                                  "predicted_per_token_ms", "predicted_per_second")},
    "tokens_cached": None, "tokens_evaluated": None,
}


class _BlockError(Exception):
    """Contains only a fixed public diagnostic code, never backend text."""


class _Deadline:
    def __init__(self, started, timeout_s):
        self.end = started + timeout_s

    def guard(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _endpoint(server):
    """No hostnames, userinfo, URL paths, scopes, proxies or redirects."""
    if not isinstance(server, str) or any(c.isspace() for c in server):
        raise ValueError
    u = urlsplit(server)
    if (u.scheme not in ("http", "https") or u.username is not None
            or u.password is not None or u.path not in ("", "/")
            or u.query or u.fragment or "?" in server or "#" in server
            or not u.hostname or "%" in u.hostname):
        raise ValueError
    address = ipaddress.ip_address(u.hostname)
    if not address.is_loopback or (address.version == 6 and address != ipaddress.IPv6Address("::1")):
        raise ValueError
    port = u.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError
    host = f"[{address}]" if address.version == 6 else str(address)
    base = f"{u.scheme}://{host}" + (f":{port}" if port is not None else "")
    return base, u.scheme, str(address), port


def _post(endpoint, path, payload, deadline):
    """Direct sockets bypass proxy env. A watchdog bounds slow headers/bodies too.

    A socket idle timeout alone is not an absolute deadline: a trickling response
    could renew it forever. Shutdown interrupts even http.client's buffered reads.
    """
    _, scheme, host, port = endpoint
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    connection = cls(host, port, timeout=deadline.guard())
    timer = None
    try:
        connection.connect()
        sock = connection.sock

        def abort():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(deadline.guard(), abort)
        timer.daemon = True
        timer.start()
        connection.request("POST", path, json.dumps(payload, allow_nan=False).encode(),
                           {"Content-Type": "application/json", "Connection": "close"})
        response = connection.getresponse()
        deadline.guard()
        if 300 <= response.status < 400:
            raise _BlockError("redirect_refused")
        if response.status != 200:
            raise _BlockError("http_error")
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        deadline.guard()
        if len(body) > _MAX_RESPONSE_BYTES:
            raise _BlockError("response_too_large")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise _BlockError("invalid_response")
        return result
    except (OSError, http.client.HTTPException):
        deadline.guard()
        raise _BlockError("transport_error") from None
    except (ValueError, UnicodeError):
        raise _BlockError("invalid_response") from None
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def _load_jev():
    """Fresh script globals per call: transport replacement is never shared.

    The vendor imports protocol modules through sys.path. Restore that path and
    fail closed if an unrelated module with the same import name was preloaded.
    No vendor functions/files or hosted entry points are modified or invoked.
    """
    script = _ROOT / "jev" / "jev"
    with _LOAD_LOCK:
        old_path = sys.path[:]
        try:
            loader = importlib.machinery.SourceFileLoader("_recall_local_jev", str(script))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
        finally:
            sys.path[:] = old_path
    protocol = (_ROOT / "jev" / "protocol").resolve()
    for name in ("hf_prompt_policies", "common", "common.request_schema",
                 "common.prompt_builder", "common.response_scoring"):
        if not Path(sys.modules[name].__file__).resolve().is_relative_to(protocol):
            raise _BlockError("protocol_import_conflict")
    if module.POLICY != "v1" or module.POLICY_NAME != "shared_examples_binary":
        raise _BlockError("protocol_mismatch")
    return module


def _escape(text):
    # Literal backslash-u sequences, NOT the Unicode characters themselves.
    # Escape data before rendering; the template's own markers stay untouched.
    return text.replace("<", r"\u003c").replace(">", r"\u003e")


def _numeric_metadata(value, schema):
    """Closed metric paths: even numeric metadata keys can echo source fragments.

    Unknown paths reject the whole metric object, explicitly flagged as omitted.
    The allowlist is independent of source text; no arbitrary backend keys leave.
    """
    if isinstance(schema, dict):
        if not isinstance(value, dict) or any(k not in schema for k in value):
            raise ValueError
        return {k: _numeric_metadata(v, schema[k]) for k, v in value.items()}
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError


def _metadata(response, row):
    for name, schema in _METADATA_SCHEMA.items():
        if name in response:
            try:
                row[name] = _numeric_metadata(response[name], schema)
            except (ValueError, RecursionError):
                row["metadata_omitted"] = True
    # Identify the actual advertised engine without allowing echoed source text.
    if isinstance(response.get("model"), str):
        row["server_model_sha256"] = _sha(response["model"])


def score_blocks(context: str, blocks: list[dict], *, server: str,
                 timeout_s: float = 90, max_prompt_tokens: int = 16384,
                 max_blocks: int | None = None) -> dict:
    """Score one candidate/request via binary JEV P(yes), with one global budget.

    Malformed input types raise a fixed ValueError. Endpoint/limit failures return
    a failed report without making requests. Scores are never synthesized for
    failed/not-attempted blocks. max_blocks=None explicitly permits all inputs;
    the effective limit is recorded. The token ceiling includes the entire
    rendered prompt, not just source text, and never truncates either input.
    """
    started = time.monotonic()
    if not isinstance(context, str) or not isinstance(blocks, list):
        raise ValueError("invalid context or blocks")
    for block in blocks:
        if (not isinstance(block, dict) or not isinstance(block.get("id"), str)
                or not isinstance(block.get("text"), str) or not isinstance(block.get("refs"), list)
                or any(type(ref) is not int for ref in block["refs"])):
            raise ValueError("invalid block")
    rows = [{"id": b["id"], "refs": list(b["refs"]), "status": "not_attempted",
             "score": None, "reason": "not_started"} for b in blocks]
    out = {"schema": "recall_relevance.scoring.v1", "rows": rows,
           "context_sha256": _sha(context), "provenance": {
               "backend": "local", "policy": "v1", "policy_name": "shared_examples_binary",
               "question": QUESTION, "question_version": QUESTION_VERSION,
               "rubric_version": RUBRIC_VERSION,
               "rubric_sha256": _sha(json.dumps(CRITERIA, sort_keys=True)),
               "state_order": ["current_context", "earlier_block"],
               "escaping": "literal-unicode-angle-brackets-v1",
               "prompt_token_count": "exact-server-tokenize",
               "completion_parameters": dict(_COMPLETION),
           }}

    def finish():
        out["total"] = len(rows)
        for status in ("scored", "failed", "not_attempted"):
            out[status] = sum(r["status"] == status for r in rows)
        out["status"] = ("complete" if out["scored"] == len(rows) else
                         "partial" if out["scored"] else "failed")
        out["reasons"] = {}
        for row in rows:
            if "reason" in row:
                reason = row["reason"]
                out["reasons"][reason] = out["reasons"].get(reason, 0) + 1
        out["elapsed_s"] = time.monotonic() - started
        return out

    def stop(reason):
        out["reason"] = reason
        for row in rows:
            row["reason"] = reason
        result = finish()
        result["status"] = "failed"
        return result

    if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0
            or type(max_prompt_tokens) is not int or max_prompt_tokens <= 0
            or (max_blocks is not None and (type(max_blocks) is not int or max_blocks < 0))):
        return stop("invalid_limits")
    try:
        endpoint = _endpoint(server)
    except (ValueError, TypeError):
        return stop("invalid_endpoint")
    out["provenance"]["server"] = endpoint[0]
    limit = len(rows) if max_blocks is None else min(max_blocks, len(rows))
    out["provenance"]["limits"] = {"timeout_s": timeout_s, "max_prompt_tokens": max_prompt_tokens,
                                  "max_blocks": max_blocks, "effective_max_blocks": limit}
    deadline = _Deadline(started, timeout_s)
    try:
        jev = _load_jev()
        template = jev.load_template()
        sources = ["jev", "template_qwen38.j2", "protocol/hf_prompt_policies.py",
                   "protocol/common/__init__.py", "protocol/common/request_schema.py",
                   "protocol/common/prompt_builder.py", "protocol/common/response_scoring.py"]
        out["provenance"]["source_sha256"] = {
            name: hashlib.sha256((_ROOT / "jev" / name).read_bytes()).hexdigest() for name in sources}
        out["provenance"]["request_schema_model"] = jev.DEFAULT_MODEL_LOCAL  # Not sent as a backend selection.
    except Exception:
        return stop("protocol_unavailable")

    escaped_context = _escape(context)
    for index, (block, row) in enumerate(zip(blocks, rows)):
        if index >= limit:
            row["reason"] = "max_blocks"
            continue
        try:
            deadline.guard()
        except TimeoutError:
            row["reason"] = "deadline_exceeded"
            continue
        row["status"] = "failed"
        t0 = time.monotonic()
        try:
            # Earlier metadata is intentionally outside the shared prefix. The
            # vendor canonical serializer sorts keys; current_context < earlier_block.
            state = {"current_context": escaped_context, "earlier_block": _escape(block["text"])}
            request = jev.build_request(state, {"recall_help": {
                "type": "noul", "instructions": QUESTION, "criteria": dict(CRITERIA)}},
                jev.DEFAULT_MODEL_LOCAL)
            plan, binary_keys = jev.hfp.prepare_policy(request, jev.POLICY, jev.POLICY_NAME)
            question = plan.questions[0]
            if (binary_keys != ("recall_help",) or len(plan.questions) != 1
                    or question.output_labels != ("A", "B") or question.answer_labels != ("no", "yes")):
                raise _BlockError("protocol_mismatch")
            out["provenance"]["plan_template_version"] = plan.template_version
            native, thinking = jev.compile_branch(request, plan, question)
            prompt = jev.render_text(native, thinking, template, question.answer_prefix)
            row["prompt_sha256"] = _sha(prompt)
            tokenization_calls = 0

            def local_post(url, payload, timeout):
                nonlocal tokenization_calls
                # Existing JEV passes remaining time, but the absolute deadline
                # below is authoritative. Only these two local routes exist.
                paths = {endpoint[0] + p: p for p in ("/tokenize", "/v1/completions")}
                if url not in paths:
                    raise _BlockError("endpoint_mismatch")
                response = _post(endpoint, paths[url], payload, deadline)
                if paths[url] == "/tokenize":
                    tokens = response.get("tokens")
                    if not isinstance(tokens, list) or any(type(t) is not int or t < 0 for t in tokens):
                        raise _BlockError("invalid_tokens")
                    tokenization_calls += 1
                    if tokenization_calls == 1:
                        row["n_tokens"] = len(tokens)
                        if len(tokens) > max_prompt_tokens:
                            raise _BlockError("max_prompt_tokens")
                else:
                    _metadata(response, row)
                    try:
                        labels = jev.extract_labels(response, question.output_labels)
                    except (KeyError, IndexError, TypeError, AttributeError):
                        raise _BlockError("invalid_logprobs") from None
                    if labels is None:
                        raise _BlockError("missing_labels")
                    bound = float(jev.np.finfo(jev.np.float32).max)
                    if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > bound for v in labels):
                        raise _BlockError("nonfinite_logprobs")
                return response

            # Only this freshly loaded module's transport globals are replaced;
            # renderer, boundary checking, label extraction and FP32 math remain vendor code.
            jev.http_post = local_post
            try:
                id_a, id_b, ntok = jev.check_boundary(endpoint[0], prompt, question.output_labels, deadline)
            except RuntimeError:
                raise _BlockError("invalid_label_boundary") from None
            row["n_tokens"] = ntok
            row["token_ids"] = {"A": id_a, "B": id_b}
            scoring_started = time.monotonic()
            lp_a, lp_b, _, _ = jev.score_local(endpoint[0], prompt, question.output_labels, deadline)
            latency = time.monotonic() - scoring_started
            with jev.np.errstate(over="ignore", invalid="ignore"):
                _, yes = jev.softmax2(lp_a, lp_b)
            if not math.isfinite(yes) or not 0 <= yes <= 1:
                raise _BlockError("nonfinite_score")
            deadline.guard()
            row.update(status="scored", score=yes, logprobs={"A": lp_a, "B": lp_b},
                       scoring_latency_s=latency)
            row.pop("reason", None)
        except TimeoutError:
            row["reason"] = "deadline_exceeded"
        except _BlockError as exc:
            row["reason"] = exc.args[0]
        except Exception:
            # Never expose a backend/template exception, which can contain text.
            row["reason"] = "scoring_error"
        finally:
            row["latency_s"] = time.monotonic() - t0
    return finish()

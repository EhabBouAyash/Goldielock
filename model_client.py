"""Calls GPT-6.1 Sol. The trading checks still live outside this file."""

from __future__ import annotations

import os
import random
import time
from typing import Any

import requests

from token_tracker import calculate_cost, log_token_usage

MODEL_NAME = "gpt-6.1-sol"
OPENAI_URL = "https://api.openai.com/v1/responses"
MAX_RETRIES = 4


def openai_api_key() -> str:
    return (os.getenv("OPENAI_API_KEY") or "").strip()


def key_status_line(env_file_name: str = ".env") -> str:
    key = openai_api_key()
    if not key:
        return f"[env] OPENAI_API_KEY missing — set it in {env_file_name}"
    return f"[env] OPENAI_API_KEY loaded from {env_file_name} (len={len(key)})"


def _retry_wait_seconds(attempt: int, response: requests.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                pass
    return min(2 ** attempt, 30) + random.uniform(0.0, 1.0)


def _is_transient(status_code: int | None, body: dict[str, Any] | None) -> bool:
    if status_code in (429, 500, 503):
        return True
    if not isinstance(body, dict):
        return False
    err = body.get("error") or {}
    err_type = str(err.get("type") or err.get("code") or "").lower()
    err_msg = str(err.get("message") or "").lower()
    if err_type in {"rate_limit_error", "server_error", "api_error"}:
        return True
    return "rate limit" in err_msg or "overloaded" in err_msg or "server error" in err_msg


def _extract_text(body: dict[str, Any], *, fail_on_truncation: bool) -> str:
    status = str(body.get("status") or "")
    incomplete = body.get("incomplete_details") or {}
    incomplete_reason = str(incomplete.get("reason") or "")
    if fail_on_truncation and status == "incomplete":
        return f"ERROR: stop_reason={incomplete_reason or 'incomplete'}"
    if status == "failed":
        err = body.get("error") or {}
        return f"ERROR: {err.get('message') or 'model run failed'}"

    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "refusal":
                return f"ERROR: stop_reason=refusal"

    text = str(body.get("output_text") or "").strip()
    if text:
        return text

    chunks: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") == "refusal":
                continue
            piece = str(part.get("text") or part.get("output_text") or "").strip()
            if piece:
                chunks.append(piece)
    joined = "\n".join(chunks).strip()
    if joined:
        return joined
    return f"ERROR: Empty response text (status={status or 'unknown'})"


def complete(
    system_prompt: str,
    user_message: str,
    *,
    mode: str,
    max_output_tokens: int,
    effort: str,
    timeout: int,
    output_schema: dict[str, Any] | None = None,
    deadline_monotonic: float | None = None,
    fail_on_truncation: bool = False,
    messages: list[dict[str, str]] | None = None,
    endpoint_label: str = "day_trading_insights",
) -> str:
    """Ask GPT-6.1 Sol. A schema-shaped reply is still not permission to trade."""
    api_key = openai_api_key()
    if not api_key:
        return "ERROR: Missing OPENAI_API_KEY in environment."

    model_input: str | list[dict[str, str]] = messages if messages is not None else user_message
    payload: dict[str, Any] = {
        "model": MODEL_NAME,
        "instructions": system_prompt,
        "input": model_input,
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": effort},
    }
    if output_schema:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": "gold_decision",
                "strict": True,
                "schema": output_schema,
            }
        }

    print("\n[MODEL CALL]")
    print(f"  Model: {MODEL_NAME} | mode={mode}")
    print(f"  effort={effort} | max_output_tokens={max_output_tokens} | timeout={timeout}s")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    last_error = "Unknown error"
    body: dict[str, Any] = {}
    for attempt in range(MAX_RETRIES):
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            return "ERROR: decision deadline elapsed before a usable model response"
        response = None
        try:
            response = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=timeout)
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            last_error = str(exc)
            if attempt >= MAX_RETRIES - 1:
                return f"ERROR: OpenAI request failed: {exc}"
            wait_s = _retry_wait_seconds(attempt, None)
            print(f"\n  Request failed ({exc}); retry {attempt + 1}/{MAX_RETRIES - 1} in {wait_s:.1f}s...")
            time.sleep(wait_s)
            continue

        if not body.get("error"):
            break

        error_obj = body.get("error") or {}
        last_error = error_obj.get("message", "Unknown error")
        status_code = getattr(response, "status_code", None)
        if _is_transient(status_code, body) and attempt < MAX_RETRIES - 1:
            wait_s = _retry_wait_seconds(attempt, response)
            print(
                f"\n  OpenAI {status_code or 'error'}: {last_error} "
                f"— retry {attempt + 1}/{MAX_RETRIES - 1} in {wait_s:.1f}s..."
            )
            time.sleep(wait_s)
            continue
        print(f"\nERROR [OpenAI HTTP {status_code}]: {last_error}")
        return f"ERROR: OpenAI {status_code}: {last_error}"
    else:
        print(f"\nERROR: {last_error} (after {MAX_RETRIES} attempts)")
        return f"ERROR: {last_error}"

    usage = body.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached_tokens = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    if usage:
        cost = calculate_cost(
            input_tokens,
            output_tokens,
            model=MODEL_NAME,
            cached_input_tokens=cached_tokens,
        )
        print("\n  Actual usage:")
        print(f"    Input tokens:        {input_tokens}")
        print(f"    Cached input tokens: {cached_tokens}")
        print(f"    Output tokens:       {output_tokens}")
        print(f"    Cost this run:       ${cost:.6f}")
        log_token_usage(
            event_type="actual",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=int(usage.get("total_tokens") or (input_tokens + output_tokens)),
            cost_usd=cost,
            details={
                "model": MODEL_NAME,
                "endpoint": endpoint_label,
                "mode": mode,
                "effort": effort,
                "max_output_tokens": max_output_tokens,
                "status": body.get("status"),
                "cached_input_tokens": cached_tokens,
            },
        )

    return _extract_text(body, fail_on_truncation=fail_on_truncation)

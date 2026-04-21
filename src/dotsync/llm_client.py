"""Thin wrapper around OpenAI-compatible chat-completion endpoints."""

from __future__ import annotations

import time

import httpx


class LLMError(Exception):
    """Raised when an LLM API call fails."""


def _base_url(endpoint: str) -> str:
    """Return the normalised base URL (no trailing slash, no ``/v1`` suffix).

    Accepts both ``http://host`` and ``http://host/v1`` so callers don't need
    to worry about the format.
    """
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _chat_url(endpoint: str) -> str:
    """Return the chat-completions URL for an endpoint."""
    return f"{_base_url(endpoint)}/v1/chat/completions"


def _models_url(endpoint: str) -> str:
    """Return the models-list URL for an endpoint."""
    return f"{_base_url(endpoint)}/v1/models"


def probe_llm(
    endpoint: str,
    model: str,
    api_key: str | None = None,
    timeout: int = 10,
) -> tuple[bool, str | None]:
    """Send a minimal chat-completion request to verify the endpoint works end-to-end.

    Tests the same path as real triage calls: POST /v1/chat/completions with
    the configured model. This catches wrong model names, bad API keys, and
    unreachable endpoints — not just TCP connectivity.

    Args:
        endpoint: Base URL of the OpenAI-compatible API.
        model: Model identifier — must be valid for the target provider.
        api_key: Optional Bearer token sent as ``Authorization: Bearer <key>``.
        timeout: HTTP request timeout in seconds.

    Returns:
        ``(True, None)`` on success; ``(False, reason)`` on any failure where
        *reason* is a human-readable description of the error.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = httpx.post(
            _chat_url(endpoint),
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        return True, None
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return False, f"HTTP {code} — check your llm_api_key"
        if code == 400:
            return False, "HTTP 400 Bad Request — check your llm_model name"
        return False, f"HTTP {code}"
    except httpx.TimeoutException:
        return False, "Request timed out"
    except httpx.ConnectError:
        return False, "Connection refused — is the endpoint running?"
    except Exception as exc:
        return False, str(exc)


def chat_completion(
    endpoint: str,
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int = 90,
    max_retries: int = 2,
    api_key: str | None = None,
) -> str:
    """Send a chat-completion request and return the assistant content.

    Retries on transient errors (timeout, HTTP errors) with exponential
    backoff. Malformed responses are not retried.

    Args:
        endpoint: Base URL of the OpenAI-compatible API (e.g. ``http://localhost:8000``).
        model: Model identifier to pass in the request body.
        system_prompt: System message content.
        user_message: User message content.
        timeout: HTTP request timeout in seconds.
        max_retries: Number of retries on transient errors (timeout, HTTP).
        api_key: Optional Bearer token sent as ``Authorization: Bearer <key>``.

    Returns:
        The assistant's reply text.

    Raises:
        LLMError: On HTTP error, timeout, or malformed response.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    last_exc: LLMError | None = None
    for attempt in range(1 + max_retries):
        if attempt > 0:
            time.sleep(2 ** attempt)
        try:
            resp = httpx.post(
                _chat_url(endpoint),
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            return body["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            last_exc = LLMError(f"HTTP {exc.response.status_code}")
            last_exc.__cause__ = exc
        except httpx.TimeoutException as exc:
            last_exc = LLMError("Request timed out")
            last_exc.__cause__ = exc
        except (KeyError, IndexError, TypeError) as exc:
            # Malformed response — not retryable
            raise LLMError(f"Malformed response: {exc}") from exc
        except httpx.HTTPError as exc:
            last_exc = LLMError(str(exc))
            last_exc.__cause__ = exc
    raise last_exc  # type: ignore[misc]

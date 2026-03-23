"""Thin wrapper around OpenAI-compatible chat-completion endpoints."""

from __future__ import annotations

import time

import httpx


class LLMError(Exception):
    """Raised when an LLM API call fails."""


def chat_completion(
    endpoint: str,
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int = 90,
    max_retries: int = 2,
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

    last_exc: LLMError | None = None
    for attempt in range(1 + max_retries):
        if attempt > 0:
            time.sleep(2 ** attempt)
        try:
            resp = httpx.post(
                f"{endpoint}/v1/chat/completions",
                json=payload,
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

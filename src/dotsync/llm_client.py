"""Thin wrapper around OpenAI-compatible chat-completion endpoints."""

from __future__ import annotations

import httpx


class LLMError(Exception):
    """Raised when an LLM API call fails."""


def chat_completion(
    endpoint: str,
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int = 15,
) -> str:
    """Send a chat-completion request and return the assistant content.

    Args:
        endpoint: Base URL of the OpenAI-compatible API (e.g. ``http://localhost:8000``).
        model: Model identifier to pass in the request body.
        system_prompt: System message content.
        user_message: User message content.
        timeout: HTTP request timeout in seconds.

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
        raise LLMError(f"HTTP {exc.response.status_code}") from exc
    except httpx.TimeoutException as exc:
        raise LLMError("Request timed out") from exc
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Malformed response: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(str(exc)) from exc

"""Tests for dotsync.llm_client module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from dotsync.llm_client import LLMError, chat_completion


class TestChatCompletion:
    """Tests for the chat_completion helper."""

    def test_chat_completion_returns_content_string(self) -> None:
        """Successful response returns the assistant content."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "hello world"}}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("dotsync.llm_client.httpx.post", return_value=mock_resp) as mock_post:
            result = chat_completion(
                endpoint="http://localhost:8000",
                model="test-model",
                system_prompt="You are helpful.",
                user_message="Hi",
            )

        assert result == "hello world"
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert call_url == "http://localhost:8000/v1/chat/completions"

    def test_chat_completion_raises_llm_error_on_http_error(self) -> None:
        """HTTP error responses raise LLMError."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error",
            request=MagicMock(),
            response=mock_resp,
        )

        with (
            patch("dotsync.llm_client.httpx.post", return_value=mock_resp),
            pytest.raises(LLMError, match="HTTP 500"),
        ):
            chat_completion(
                endpoint="http://localhost:8000",
                model="test-model",
                system_prompt="sys",
                user_message="msg",
            )

    def test_chat_completion_raises_llm_error_on_timeout(self) -> None:
        """Timeout raises LLMError."""
        with (
            patch(
                "dotsync.llm_client.httpx.post",
                side_effect=httpx.ReadTimeout("timed out"),
            ),
            pytest.raises(LLMError, match="timed out"),
        ):
            chat_completion(
                endpoint="http://localhost:8000",
                model="test-model",
                system_prompt="sys",
                user_message="msg",
            )

    def test_chat_completion_raises_llm_error_on_missing_choices(self) -> None:
        """Malformed response without 'choices' raises LLMError."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"no_choices": True}
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("dotsync.llm_client.httpx.post", return_value=mock_resp),
            pytest.raises(LLMError, match="Malformed response"),
        ):
            chat_completion(
                endpoint="http://localhost:8000",
                model="test-model",
                system_prompt="sys",
                user_message="msg",
            )

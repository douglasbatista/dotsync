"""Tests for dotsync.llm_client module."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from dotsync.llm_client import LLMError, chat_completion


def _ok_response(content: str = "hello world") -> MagicMock:
    """Build a mock httpx.Response with a valid chat-completion body."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
    }
    resp.raise_for_status = MagicMock()
    return resp


class TestChatCompletion:
    """Tests for the chat_completion helper."""

    def test_chat_completion_returns_content_string(self) -> None:
        """Successful response returns the assistant content."""
        with patch("dotsync.llm_client.httpx.post", return_value=_ok_response()) as mock_post:
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
        """HTTP error responses raise LLMError after exhausting retries."""
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
                max_retries=0,
            )

    def test_chat_completion_raises_llm_error_on_timeout(self) -> None:
        """Timeout raises LLMError after exhausting retries."""
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
                max_retries=0,
            )

    def test_chat_completion_accepts_positional_timeout(self) -> None:
        """Timeout can be passed as a positional argument."""
        with patch("dotsync.llm_client.httpx.post", return_value=_ok_response("ok")) as mock_post:
            result = chat_completion(
                "http://localhost:8000",
                "test-model",
                "You are helpful.",
                "Hi",
                5,
            )

        assert result == "ok"
        mock_post.assert_called_once()
        assert mock_post.call_args[1]["timeout"] == 5

    def test_chat_completion_raises_llm_error_on_missing_choices(self) -> None:
        """Malformed response without 'choices' raises LLMError immediately (no retry)."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"no_choices": True}
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("dotsync.llm_client.httpx.post", return_value=mock_resp) as mock_post,
            pytest.raises(LLMError, match="Malformed response"),
        ):
            chat_completion(
                endpoint="http://localhost:8000",
                model="test-model",
                system_prompt="sys",
                user_message="msg",
            )

        # Malformed responses are not retried
        mock_post.assert_called_once()


class TestRetryBehaviour:
    """Tests for retry and backoff logic."""

    def test_retries_on_timeout_then_succeeds(self) -> None:
        """Retries on timeout and returns result on success."""
        mock_post = MagicMock(
            side_effect=[
                httpx.ReadTimeout("timed out"),
                httpx.ReadTimeout("timed out"),
                _ok_response("recovered"),
            ]
        )
        with (
            patch("dotsync.llm_client.httpx.post", mock_post),
            patch("dotsync.llm_client.time.sleep") as mock_sleep,
        ):
            result = chat_completion(
                endpoint="http://localhost:8000",
                model="m",
                system_prompt="s",
                user_message="u",
                max_retries=2,
            )

        assert result == "recovered"
        assert mock_post.call_count == 3
        mock_sleep.assert_has_calls([call(2), call(4)])

    def test_retries_on_http_error_then_succeeds(self) -> None:
        """Retries on HTTP 503 and returns result on success."""
        bad_resp = MagicMock(spec=httpx.Response)
        bad_resp.status_code = 503
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Service Unavailable", request=MagicMock(), response=bad_resp,
        )

        mock_post = MagicMock(side_effect=[bad_resp, _ok_response("ok")])
        with (
            patch("dotsync.llm_client.httpx.post", mock_post),
            patch("dotsync.llm_client.time.sleep"),
        ):
            result = chat_completion(
                endpoint="http://localhost:8000",
                model="m",
                system_prompt="s",
                user_message="u",
                max_retries=1,
            )

        assert result == "ok"
        assert mock_post.call_count == 2

    def test_exhausts_retries_then_raises(self) -> None:
        """Raises LLMError after exhausting all retries."""
        mock_post = MagicMock(side_effect=httpx.ReadTimeout("timed out"))
        with (
            patch("dotsync.llm_client.httpx.post", mock_post),
            patch("dotsync.llm_client.time.sleep"),
            pytest.raises(LLMError, match="timed out"),
        ):
            chat_completion(
                endpoint="http://localhost:8000",
                model="m",
                system_prompt="s",
                user_message="u",
                max_retries=2,
            )

        # 1 initial + 2 retries = 3 total
        assert mock_post.call_count == 3

    def test_no_retry_on_malformed_response(self) -> None:
        """Malformed response raises immediately without retry."""
        bad_resp = MagicMock(spec=httpx.Response)
        bad_resp.status_code = 200
        bad_resp.json.return_value = {"no_choices": True}
        bad_resp.raise_for_status = MagicMock()

        mock_post = MagicMock(return_value=bad_resp)
        with (
            patch("dotsync.llm_client.httpx.post", mock_post),
            patch("dotsync.llm_client.time.sleep") as mock_sleep,
            pytest.raises(LLMError, match="Malformed response"),
        ):
            chat_completion(
                endpoint="http://localhost:8000",
                model="m",
                system_prompt="s",
                user_message="u",
                max_retries=2,
            )

        mock_post.assert_called_once()
        mock_sleep.assert_not_called()

    def test_backoff_timing(self) -> None:
        """Exponential backoff sleeps 2s then 4s."""
        mock_post = MagicMock(side_effect=httpx.ReadTimeout("timed out"))
        with (
            patch("dotsync.llm_client.httpx.post", mock_post),
            patch("dotsync.llm_client.time.sleep") as mock_sleep,
            pytest.raises(LLMError),
        ):
            chat_completion(
                endpoint="http://localhost:8000",
                model="m",
                system_prompt="s",
                user_message="u",
                max_retries=2,
            )

        assert mock_sleep.call_args_list == [call(2), call(4)]

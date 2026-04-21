"""Tests for dotsync.llm_client module."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from dotsync.llm_client import LLMError, _chat_url, chat_completion, probe_llm


def _ok_response(content: str = "hello world") -> MagicMock:
    """Build a mock httpx.Response with a valid chat-completion body."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
    }
    resp.raise_for_status = MagicMock()
    return resp


def _ok_probe_response() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    return resp


class TestChatUrl:
    """Tests for the _chat_url URL normalisation helper."""

    def test_plain_host(self) -> None:
        assert _chat_url("http://localhost:8000") == "http://localhost:8000/v1/chat/completions"

    def test_trailing_slash_stripped(self) -> None:
        assert _chat_url("http://localhost:8000/") == "http://localhost:8000/v1/chat/completions"

    def test_v1_suffix_stripped(self) -> None:
        assert _chat_url("http://localhost:8000/v1") == "http://localhost:8000/v1/chat/completions"

    def test_v1_trailing_slash_stripped(self) -> None:
        assert _chat_url("http://localhost:8000/v1/") == "http://localhost:8000/v1/chat/completions"

    def test_https_with_v1(self) -> None:
        assert _chat_url("https://host.example.com/v1") == "https://host.example.com/v1/chat/completions"


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


class TestApiKey:
    """Tests that api_key is sent as a Bearer token."""

    def test_chat_completion_sends_bearer_token(self) -> None:
        with patch("dotsync.llm_client.httpx.post", return_value=_ok_response()) as mock_post:
            chat_completion(
                endpoint="http://localhost:8000",
                model="m",
                system_prompt="s",
                user_message="u",
                api_key="secret-key",
            )

        headers = mock_post.call_args[1]["headers"]
        assert headers == {"Authorization": "Bearer secret-key"}

    def test_chat_completion_no_auth_header_when_no_key(self) -> None:
        with patch("dotsync.llm_client.httpx.post", return_value=_ok_response()) as mock_post:
            chat_completion(
                endpoint="http://localhost:8000",
                model="m",
                system_prompt="s",
                user_message="u",
            )

        headers = mock_post.call_args[1]["headers"]
        assert headers == {}

    def test_probe_llm_sends_bearer_token(self) -> None:
        with patch("dotsync.llm_client.httpx.post", return_value=_ok_probe_response()) as mock_post:
            ok, _ = probe_llm("http://localhost:8000", "m", api_key="tok")

        assert ok is True
        headers = mock_post.call_args[1]["headers"]
        assert headers == {"Authorization": "Bearer tok"}

    def test_probe_llm_no_auth_header_when_no_key(self) -> None:
        with patch("dotsync.llm_client.httpx.post", return_value=_ok_probe_response()) as mock_post:
            ok, _ = probe_llm("http://localhost:8000", "m")

        assert ok is True
        headers = mock_post.call_args[1]["headers"]
        assert headers == {}


class TestProbeLlm:
    """Tests for the probe_llm connectivity check."""

    def test_returns_true_none_on_valid_response(self) -> None:
        """Returns (True, None) when the endpoint replies successfully."""
        with patch("dotsync.llm_client.httpx.post", return_value=_ok_probe_response()):
            ok, reason = probe_llm("http://localhost:8000", "test-model")
        assert ok is True
        assert reason is None

    def test_returns_false_with_reason_on_http_503(self) -> None:
        """Returns (False, 'HTTP 503') for a generic server error."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 503
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Service Unavailable", request=MagicMock(), response=mock_resp
        )

        with patch("dotsync.llm_client.httpx.post", return_value=mock_resp):
            ok, reason = probe_llm("http://localhost:8000", "test-model")
        assert ok is False
        assert reason == "HTTP 503"

    def test_returns_false_with_reason_on_401(self) -> None:
        """Returns (False, reason mentioning llm_api_key) on 401 Unauthorized."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized", request=MagicMock(), response=mock_resp
        )

        with patch("dotsync.llm_client.httpx.post", return_value=mock_resp):
            ok, reason = probe_llm("http://localhost:8000", "test-model")
        assert ok is False
        assert reason is not None
        assert "401" in reason
        assert "llm_api_key" in reason

    def test_returns_false_with_reason_on_403(self) -> None:
        """Returns (False, reason mentioning llm_api_key) on 403 Forbidden."""
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 403
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Forbidden", request=MagicMock(), response=mock_resp
        )

        with patch("dotsync.llm_client.httpx.post", return_value=mock_resp):
            ok, reason = probe_llm("http://localhost:8000", "test-model")
        assert ok is False
        assert reason is not None
        assert "403" in reason
        assert "llm_api_key" in reason

    def test_returns_false_on_timeout(self) -> None:
        """Returns (False, reason) when the request times out."""
        with patch(
            "dotsync.llm_client.httpx.post",
            side_effect=httpx.ConnectTimeout("timed out"),
        ):
            ok, reason = probe_llm("http://localhost:8000", "test-model")
        assert ok is False
        assert reason is not None

    def test_returns_false_on_connection_error(self) -> None:
        """Returns (False, reason) when the endpoint is unreachable."""
        with patch(
            "dotsync.llm_client.httpx.post",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            ok, reason = probe_llm("http://localhost:8000", "test-model")
        assert ok is False
        assert reason is not None

    def test_uses_custom_timeout(self) -> None:
        """Passes the timeout argument to httpx.post."""
        with patch("dotsync.llm_client.httpx.post", return_value=_ok_probe_response()) as mock_post:
            probe_llm("http://localhost:8000", "test-model", timeout=3)
        assert mock_post.call_args[1]["timeout"] == 3

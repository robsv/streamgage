"""Retrying requests that the services refuse for being too busy."""

import pytest

import streamgage as sg


class Reply:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.headers, self._body = status, headers or {}, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sg.requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._body

    @property
    def text(self):
        return self._body


@pytest.fixture
def served(monkeypatch):
    """Make session.get return the given replies in order; returns (calls, sleeps)."""
    def install(*replies):
        calls, sleeps, queue = [], [], list(replies)
        monkeypatch.setattr(sg.session, "get", lambda url, params, timeout: calls.append(url) or queue.pop(0))
        monkeypatch.setattr(sg.time, "sleep", sleeps.append)
        return calls, sleeps
    return install


def test_a_good_answer_is_returned_without_waiting(served):
    calls, sleeps = served(Reply(200, {"ok": 1}))
    assert sg.get_json("https://x/y") == {"ok": 1}
    assert len(calls) == 1 and sleeps == []


def test_a_rate_limit_is_retried_with_growing_waits(served):
    calls, sleeps = served(Reply(429), Reply(429), Reply(200, {"ok": 1}))
    assert sg.get_json("https://x/y") == {"ok": 1}
    assert len(calls) == 3 and sleeps == [2, 5]


def test_retry_after_is_honored(served):
    _, sleeps = served(Reply(429, headers={"Retry-After": "12"}),
                       Reply(503, headers={"Retry-After": str(sg.MAX_RETRY_AFTER)}), Reply(200, "text"))
    assert sg.get_text("https://x/y") == "text"
    assert sleeps == [12, sg.MAX_RETRY_AFTER]


def test_a_retry_after_longer_than_the_cap_fails_at_once_and_says_how_long(served):
    calls, sleeps = served(Reply(429, headers={"Retry-After": "701"}))
    with pytest.raises(sg.requests.HTTPError, match=r"asks to wait 701 s \(about 12 minutes\)") as exc:
        sg.get_json("https://x/y?secret=1")
    assert len(calls) == 1 and sleeps == []  # nothing is waited for, nothing retried
    assert exc.value.response.status_code == 429
    assert "secret" not in str(exc.value)  # the query is not echoed
    assert sg.is_refusal(exc.value)  # so the NWS fallback and the like still treat it as a refusal


def test_gives_up_after_the_last_retry(served):
    calls, sleeps = served(*[Reply(429)] * (len(sg.RETRY_DELAYS) + 1))
    with pytest.raises(sg.requests.HTTPError):
        sg.get_json("https://x/y")
    assert len(calls) == len(sg.RETRY_DELAYS) + 1 and sleeps == list(sg.RETRY_DELAYS)


@pytest.mark.parametrize("status", [400, 404, 500])
def test_other_errors_are_raised_at_once(served, status):
    calls, sleeps = served(Reply(status))
    with pytest.raises(sg.requests.HTTPError):
        sg.get_json("https://x/y")
    assert len(calls) == 1 and sleeps == []

import pytest
from slack_sdk.errors import SlackApiError

from utils import slack


class FakeResponse(dict):
    """Minimal stand-in for a slack_sdk response (dict-like, with .data)."""

    @property
    def data(self):
        return self


def _api_error(code):
    return SlackApiError(f'error: {code}', FakeResponse(ok=False, error=code))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Keep retry backoff from actually sleeping during tests."""
    monkeypatch.setattr(slack.time, 'sleep', lambda seconds: None)


def test_with_retries_returns_on_success():
    assert slack._with_retries(lambda: 'ok') == 'ok'


def test_with_retries_recovers_from_transient_error():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _api_error('file_update_failed')
        return 'ok'

    assert slack._with_retries(flaky) == 'ok'
    assert len(calls) == 3


def test_with_retries_gives_up_after_max_attempts():
    calls = []

    def always_failing():
        calls.append(1)
        raise _api_error('internal_error')

    with pytest.raises(SlackApiError):
        slack._with_retries(always_failing, max_attempts=3)
    assert len(calls) == 3


def test_with_retries_does_not_retry_permanent_errors():
    calls = []

    def bad_auth():
        calls.append(1)
        raise _api_error('invalid_auth')

    with pytest.raises(SlackApiError):
        slack._with_retries(bad_auth)
    assert len(calls) == 1  # No point retrying a bad token


def test_with_retries_passes_arguments_through():
    assert slack._with_retries(lambda a, b=None: (a, b), 1, b=2) == (1, 2)


class FakeClient:
    """Client returning canned conversations_list pages."""

    def __init__(self, pages):
        self.pages = pages
        self.cursors_seen = []

    def conversations_list(self, limit=None, cursor=None):
        self.cursors_seen.append(cursor)
        page = self.pages[len(self.cursors_seen) - 1]
        return FakeResponse(page)


def test_get_channel_id_finds_channel_on_first_page():
    client = FakeClient([{'channels': [{'name': 'hudson-conditions', 'id': 'C1'}]}])
    assert slack.get_channel_id(client, 'hudson-conditions') == 'C1'


def test_get_channel_id_pages_until_found():
    # Regression: conversations_list returns one page at a time, so a channel
    # past the first page used to be reported as nonexistent.
    client = FakeClient([
        {'channels': [{'name': 'general', 'id': 'C0'}], 'response_metadata': {'next_cursor': 'page2'}},
        {'channels': [{'name': 'hudson-conditions', 'id': 'C1'}], 'response_metadata': {'next_cursor': ''}},
    ])
    assert slack.get_channel_id(client, 'hudson-conditions') == 'C1'
    assert client.cursors_seen == [None, 'page2']


def test_get_channel_id_raises_with_useful_message():
    client = FakeClient([{'channels': [{'name': 'general', 'id': 'C0'}]}])
    with pytest.raises(ValueError, match="No Slack channel named 'nope'"):
        slack.get_channel_id(client, 'nope')

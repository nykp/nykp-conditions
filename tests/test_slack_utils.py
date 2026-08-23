import pytest
from slack_sdk.errors import SlackApiError

from nykp_conditions.utils import slack


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


class FakeUploadClient(FakeClient):
    """Records what files_upload_v2 was called with and echoes back a response."""

    def __init__(self, channels):
        super().__init__(channels)
        self.upload_kwargs = None

    def files_upload_v2(self, **kwargs):
        self.upload_kwargs = kwargs
        uploads = kwargs['file_uploads']
        return FakeResponse(files=[{'id': f'F{i}', 'title': u['title']}
                                   for i, u in enumerate(uploads)])


def test_post_files_uploads_everything_in_one_call():
    # One call means one message in the channel; uploading a file at a time
    # would split a section's images apart.
    client = FakeUploadClient([{'channels': [{'name': 'hudson-conditions', 'id': 'C1'}]}])
    files = slack.post_files(
        [('/tmp/map.png', 'Map'), ('/tmp/rain.png', 'Rain')],
        channel='hudson-conditions', comment='hello', client=client,
    )

    assert [f['title'] for f in files] == ['Map', 'Rain']
    assert client.upload_kwargs['file_uploads'] == [
        {'file': '/tmp/map.png', 'title': 'Map'},
        {'file': '/tmp/rain.png', 'title': 'Rain'},
    ]
    assert client.upload_kwargs['channel'] == 'C1'
    assert client.upload_kwargs['initial_comment'] == 'hello'


def test_post_file_returns_the_single_file():
    # files_upload_v2 always returns a `files` list; the singular `file` key
    # only appears when exactly one was uploaded, so post_file reads the list.
    client = FakeUploadClient([{'channels': [{'name': 'hudson-conditions', 'id': 'C1'}]}])
    file = slack.post_file('/tmp/map.png', title='Map', channel='hudson-conditions',
                           client=client)
    assert file['title'] == 'Map'


class FakeMessageClient:
    """Records chat_postMessage calls."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def chat_postMessage(self, **kwargs):
        if self.error:
            raise _api_error(self.error)
        self.calls.append(kwargs)
        return FakeResponse(ok=True, ts='1787319706.147309')


def test_post_message_sends_text_and_channel():
    client = FakeMessageClient()
    slack.post_message('hello', channel='hudson-conditions', client=client)
    assert client.calls == [{'channel': 'hudson-conditions', 'text': 'hello'}]


def test_post_message_passes_extra_kwargs_through():
    client = FakeMessageClient()
    slack.post_message('hi', channel='c', client=client, unfurl_links=False)
    assert client.calls[0]['unfurl_links'] is False


def test_post_message_reraises_after_reporting(capsys):
    client = FakeMessageClient(error='channel_not_found')
    with pytest.raises(SlackApiError):
        slack.post_message('hi', channel='nope', client=client)
    assert 'channel_not_found' in capsys.readouterr().out


def test_post_message_retries_transient_errors():
    # post_message routes through _with_retries, so a transient failure on the
    # first attempt shouldn't lose the message.
    attempts = []

    class Flaky(FakeMessageClient):
        def chat_postMessage(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise _api_error('internal_error')
            return FakeResponse(ok=True)

    slack.post_message('hi', channel='c', client=Flaky())
    assert len(attempts) == 2


def test_post_text_attachment_bolds_the_pretext():
    client = FakeMessageClient()
    slack.post_text_attachment('the body', pretext='Waterbody Advisory',
                               channel='c', client=client)
    call = client.calls[0]
    assert call['text'] == '*Waterbody Advisory*'
    assert call['attachments'][0].text == 'the body'


def test_post_text_attachment_keeps_body_out_of_the_top_level_text():
    client = FakeMessageClient()
    slack.post_text_attachment('the body', pretext='head', channel='c', client=client)
    assert 'the body' not in client.calls[0]['text']


def test_get_client_uses_the_bot_token_by_default(monkeypatch):
    monkeypatch.setattr(slack, 'get_slack_bot_token', lambda: 'xoxb-test')
    monkeypatch.setattr(slack, 'get_slack_user_token',
                        lambda: pytest.fail('should not need the user token'))
    assert slack.get_client().token == 'xoxb-test'


def test_get_client_can_ask_for_the_user_token(monkeypatch):
    monkeypatch.setattr(slack, 'get_slack_user_token', lambda: 'xoxp-test')
    assert slack.get_client(user=True).token == 'xoxp-test'


def test_get_client_prefers_an_explicit_token(monkeypatch):
    monkeypatch.setattr(slack, 'get_slack_bot_token',
                        lambda: pytest.fail('should not look up a token'))
    assert slack.get_client(token='xoxb-explicit').token == 'xoxb-explicit'

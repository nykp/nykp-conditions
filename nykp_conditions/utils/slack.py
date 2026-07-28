import time
from typing import Callable, Optional

import slack_sdk
from slack_sdk.errors import SlackApiError
from slack_sdk.models.attachments import Attachment

from .secrets import get_slack_bot_token, get_slack_user_token


# Slack API errors that are transient/server-side and worth retrying.
# `file_update_failed` in particular shows up intermittently when finalizing
# an upload via files_upload_v2.
RETRIABLE_SLACK_ERRORS = frozenset({
    'file_update_failed',
    'internal_error',
    'fatal_error',
    'service_unavailable',
})


class NykpSlackChannels:
    test_python_api = 'test-python-api'
    hudson_conditions = 'hudson-conditions'
    hudson_sessions = 'hudson-sessions'


def _with_retries(func: Callable, *args, max_attempts: int = 3, base_delay: float = 2.0, **kwargs):
    """Call a Slack API method, retrying with exponential backoff on transient errors."""
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except SlackApiError as e:
            error = e.response.get('error')
            if error in RETRIABLE_SLACK_ERRORS and attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"Slack error {error!r} (attempt {attempt}/{max_attempts}); retrying in {delay:.0f}s")
                time.sleep(delay)
                continue
            raise


def get_client(token=None, user=False):
    if token is None:
        if user:
            token = get_slack_user_token()
        else:
            token = get_slack_bot_token()
    return slack_sdk.WebClient(token=token)


def get_channel_id(client: slack_sdk.WebClient, name: str) -> str:
    channel_map = {}
    for channel in client.conversations_list()['channels']:
        try:
            if channel['name'] in channel_map:
                raise ValueError(f"Channel already in channel map: {repr(channel['name'])}")
            channel_map[channel['name']] = channel['id']
        except:
            pass
    return channel_map[name]


def post_message(text: Optional[str], channel=NykpSlackChannels.test_python_api, client=None, token=None, **kwargs):
    try:
        if client is None:
            client = get_client(token=token)
        return _with_retries(
            client.chat_postMessage,
            channel=channel,
            text=text,
            **kwargs
        )
    except SlackApiError as e:
        # You will get a SlackApiError if "ok" is False
        print(f"SlackApiError: {e.response['error']}")  # something like 'invalid_auth', 'channel_not_found'
        raise


def post_text_attachment(
        text: str, pretext: Optional[str] = None, channel=NykpSlackChannels.test_python_api, client=None,
        token=None, **kwargs
):
    bold_pretext = f"*{pretext}*"
    attachment = Attachment(text=text)
    return post_message(text=bold_pretext, channel=channel, client=client, token=token, attachments=[attachment],
                        **kwargs)


def post_file(path: str, title: str, channel: str, comment=None, client=None, token=None) -> dict:
    if client is None:
        client = get_client(token=token)
    channel_id = get_channel_id(client, channel)
    resp = _with_retries(
        client.files_upload_v2, file=path, title=title, channel=channel_id, initial_comment=comment
    )
    return resp.data['file']

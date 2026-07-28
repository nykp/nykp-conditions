"""Slack token lookup: environment variable in CI, local file otherwise."""

import os

SECRETS_DIR = os.path.abspath(os.path.join(__file__, '../../../.secrets'))
SLACK_BOT_TOKEN_FILE = os.path.join(SECRETS_DIR, 'slack_app_bot_token')
SLACK_USER_TOKEN_FILE = os.path.join(SECRETS_DIR, 'slack_app_user_token')


def _get_token(env_var, path):
    """Read a token from the environment, falling back to a local file."""
    # Prefer an environment variable (e.g. in CI); fall back to the local
    # .secrets file for interactive/local use.
    token = os.environ.get(env_var)
    if token:
        return token.strip()
    try:
        with open(path) as f:
            return f.readlines()[0].strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"No Slack token found: set ${env_var} or create {path}"
        ) from None


def get_slack_bot_token():
    """Bot token, used for all posting."""
    return _get_token('SLACK_BOT_TOKEN', SLACK_BOT_TOKEN_FILE)


def get_slack_user_token():
    """User token, only needed for API calls the bot token can't make."""
    return _get_token('SLACK_USER_TOKEN', SLACK_USER_TOKEN_FILE)

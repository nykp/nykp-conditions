import os

SECRETS_DIR = os.path.abspath(os.path.join(__file__, '../../../.secrets'))
SLACK_BOT_TOKEN_FILE = os.path.join(SECRETS_DIR, 'slack_app_bot_token')
SLACK_USER_TOKEN_FILE = os.path.join(SECRETS_DIR, 'slack_app_user_token')


def _get_token(env_var, path):
    # Prefer an environment variable (e.g. in CI); fall back to the local
    # .secrets file for interactive/local use.
    token = os.environ.get(env_var)
    if token:
        return token.strip()
    with open(path) as f:
        return f.readlines()[0].strip()


def get_slack_bot_token():
    return _get_token('SLACK_BOT_TOKEN', SLACK_BOT_TOKEN_FILE)


def get_slack_user_token():
    return _get_token('SLACK_USER_TOKEN', SLACK_USER_TOKEN_FILE)

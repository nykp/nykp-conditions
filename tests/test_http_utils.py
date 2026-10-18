import io
import json
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from nykp_conditions.utils import http


class FakeResponse(io.BytesIO):
    headers = Message()


def _http_error(code):
    return HTTPError('https://example.com', code, 'error', Message(), None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Keep retry backoff from actually sleeping during tests."""
    sleeps = []
    monkeypatch.setattr(http.time, 'sleep', sleeps.append)
    return sleeps


def _stub_urlopen(monkeypatch, *outcomes):
    """Have the underlying urlopen raise or return each outcome in turn."""
    calls = []

    def fake_urlopen(url, timeout):
        outcome = outcomes[len(calls)]
        calls.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(http, '_urlopen', fake_urlopen)
    return calls


def test_urlopen_returns_the_body(monkeypatch):
    _stub_urlopen(monkeypatch, b'{"ok": true}')
    with http.urlopen('https://example.com') as resp:
        assert json.load(resp) == {'ok': True}


def test_urlopen_retries_a_server_error(monkeypatch, no_sleep):
    calls = _stub_urlopen(monkeypatch, _http_error(500), URLError('reset'), b'ok')
    assert http.urlopen('https://example.com').read() == b'ok'
    assert len(calls) == 3
    assert no_sleep == list(http.RETRY_DELAYS[:2])


def test_urlopen_gives_up_after_the_last_retry(monkeypatch):
    errors = [_http_error(503)] * (len(http.RETRY_DELAYS) + 1)
    calls = _stub_urlopen(monkeypatch, *errors)
    with pytest.raises(HTTPError):
        http.urlopen('https://example.com')
    assert len(calls) == len(errors)


def test_urlopen_does_not_retry_a_client_error(monkeypatch):
    calls = _stub_urlopen(monkeypatch, _http_error(404), b'unreachable')
    with pytest.raises(HTTPError):
        http.urlopen('https://example.com')
    assert len(calls) == 1


def test_urlretrieve_writes_the_body(monkeypatch, tmp_path):
    _stub_urlopen(monkeypatch, TimeoutError('slow'), b'csv,data\n')
    path, _ = http.urlretrieve('https://example.com', str(tmp_path / 'out.csv'))
    assert open(path).read() == 'csv,data\n'

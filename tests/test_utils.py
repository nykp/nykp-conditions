import numpy as np
import pandas as pd
import pytest

from utils import secrets
from utils.data import rename_cols
from utils.scripts import optional, str2bool
from utils.units import knots_to_mph


@pytest.mark.parametrize('value, expected', [
    ('true', True), ('True', True), ('t', True), ('yes', True), ('y', True), ('1', True), ('on', True),
    ('false', False), ('FALSE', False), ('f', False), ('no', False), ('n', False), ('0', False), ('off', False),
    (True, True), (False, False), (None, None),
])
def test_str2bool(value, expected):
    assert str2bool(value) is expected


def test_str2bool_rejects_ambiguous():
    with pytest.raises(ValueError):
        str2bool('maybe')


def test_str2bool_can_ignore_errors():
    assert str2bool('maybe', ignore_errors=True) == 'maybe'


def test_optional_maps_none_strings():
    to_int = optional(int)
    assert to_int('none') is None
    assert to_int('NULL') is None
    assert to_int('7') == 7


@pytest.mark.parametrize('knots, expected', [
    (1.0, 1.15078),
    ('2.0', 2.30156),
    (0, 0.0),
    (-1.0, -1.15078),
])
def test_knots_to_mph(knots, expected):
    assert knots_to_mph(knots) == pytest.approx(expected)


@pytest.mark.parametrize('bad', [None, '', 'n/a'])
def test_knots_to_mph_returns_nan_for_missing_readings(bad):
    assert np.isnan(knots_to_mph(bad))


def test_rename_cols_matches_on_substring():
    df = pd.DataFrame({'Date_Time (LST_LDT)': [1], 'Speed (knots)': [2]})
    renamed = rename_cols(df, {'Date_Time': 'datetime', 'Speed': 'knots'})
    assert list(renamed.columns) == ['datetime', 'knots']


def test_rename_cols_raises_when_no_match():
    df = pd.DataFrame({'Speed (knots)': [2]})
    with pytest.raises(KeyError, match='No column matching'):
        rename_cols(df, {'Direction': 'direction'})


def test_rename_cols_raises_when_ambiguous():
    df = pd.DataFrame({'Speed (knots)': [1], 'Speed (mph)': [2]})
    with pytest.raises(ValueError, match='Multiple matches'):
        rename_cols(df, {'Speed': 'speed'})


def test_get_token_prefers_environment(monkeypatch, tmp_path):
    token_file = tmp_path / 'token'
    token_file.write_text('from-file\n')
    monkeypatch.setenv('TEST_TOKEN', '  from-env  ')
    assert secrets._get_token('TEST_TOKEN', str(token_file)) == 'from-env'


def test_get_token_falls_back_to_file(monkeypatch, tmp_path):
    token_file = tmp_path / 'token'
    token_file.write_text('from-file\nignored second line\n')
    monkeypatch.delenv('TEST_TOKEN', raising=False)
    assert secrets._get_token('TEST_TOKEN', str(token_file)) == 'from-file'


def test_get_token_error_names_both_sources(monkeypatch, tmp_path):
    monkeypatch.delenv('TEST_TOKEN', raising=False)
    missing = str(tmp_path / 'nope')
    with pytest.raises(RuntimeError, match=r'set \$TEST_TOKEN'):
        secrets._get_token('TEST_TOKEN', missing)

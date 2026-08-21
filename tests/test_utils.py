import numpy as np
import pandas as pd
import pytest

from argparse import ArgumentParser

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter

from utils import secrets
from utils.data import rename_cols
from utils.plot import DateTimeFormats, format_time_axis
from utils.scripts import optional, str2bool, try_main
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


# ---------------------------------------------------------------- utils.plot

@pytest.fixture
def ax():
    fig, ax = plt.subplots()
    yield ax
    plt.close(fig)


@pytest.mark.parametrize('axis', [0, 'x', 'X'])
def test_format_time_axis_targets_x(ax, axis):
    format_time_axis(ax, fmt=DateTimeFormats.y_m_d, axis=axis)
    assert isinstance(ax.xaxis.get_major_formatter(), DateFormatter)
    assert not isinstance(ax.yaxis.get_major_formatter(), DateFormatter)


@pytest.mark.parametrize('axis', [1, 'y', 'Y'])
def test_format_time_axis_targets_y(ax, axis):
    format_time_axis(ax, fmt=DateTimeFormats.y_m_d, axis=axis)
    assert isinstance(ax.yaxis.get_major_formatter(), DateFormatter)
    assert not isinstance(ax.xaxis.get_major_formatter(), DateFormatter)


def test_format_time_axis_rejects_unknown_axis(ax):
    with pytest.raises(ValueError, match='Unrecognized axis'):
        format_time_axis(ax, axis='z')


def test_format_time_axis_defaults_to_current_axes():
    fig, ax = plt.subplots()
    try:
        plt.sca(ax)
        format_time_axis(fmt=DateTimeFormats.y)
        assert isinstance(ax.xaxis.get_major_formatter(), DateFormatter)
    finally:
        plt.close(fig)


def test_format_time_axis_applies_the_requested_format(ax):
    format_time_axis(ax, fmt=DateTimeFormats.y_m)
    formatted = ax.xaxis.get_major_formatter()(
        matplotlib.dates.date2num(pd.Timestamp('2026-08-21 13:45:00'))
    )
    assert formatted == '2026-08'


# ------------------------------------------------------------- utils.scripts

def _parser():
    return ArgumentParser()


def test_try_main_runs_main_with_parsed_args(monkeypatch):
    monkeypatch.setattr('sys.argv', ['prog'])
    seen = []
    try_main(lambda args: seen.append(args), _parser())
    assert len(seen) == 1


def test_try_main_reraises_without_postmortem(monkeypatch):
    monkeypatch.setattr('sys.argv', ['prog'])

    def boom(args):
        raise RuntimeError('upstream is down')

    with pytest.raises(RuntimeError, match='upstream is down'):
        try_main(boom, _parser())


def test_try_main_swallows_keyboard_interrupt(monkeypatch):
    # A Ctrl-C at the terminal shouldn't print a traceback.
    monkeypatch.setattr('sys.argv', ['prog'])

    def interrupted(args):
        raise KeyboardInterrupt

    try_main(interrupted, _parser())  # Does not raise


def test_try_main_enters_postmortem_on_failure(monkeypatch):
    monkeypatch.setattr('sys.argv', ['prog', '--postmortem'])
    entered = []
    monkeypatch.setattr('pdb.post_mortem', lambda: entered.append(True))

    def boom(args):
        raise RuntimeError('down')

    try_main(boom, _parser())  # Swallowed, having been handed to pdb
    assert entered == [True]


def test_try_main_breaks_on_entry_with_pdb(monkeypatch):
    monkeypatch.setattr('sys.argv', ['prog', '--pdb'])
    traced = []
    monkeypatch.setattr('pdb.set_trace', lambda: traced.append(True))
    try_main(lambda args: None, _parser())
    assert traced == [True]


def test_try_main_keeps_the_parsers_own_arguments(monkeypatch):
    monkeypatch.setattr('sys.argv', ['prog', '--channel', 'test-python-api'])
    parser = _parser()
    parser.add_argument('--channel')
    seen = []
    try_main(lambda args: seen.append(args.channel), parser)
    assert seen == ['test-python-api']

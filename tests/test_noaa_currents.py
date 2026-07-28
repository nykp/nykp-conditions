import os

import pandas as pd
import pendulum
import pytest

import noaa_currents as nc


@pytest.mark.parametrize('value, expected', [
    (None, 1),
    (1, 1),
    (2, 2),
    ('1', 1),
    ('2', 2),
    ('48h', 1),
    ('2d', 1),
    ('w', 2),
    ('1w', 2),
    ('WEEKLY', 2),
])
def test_parse_time_period_accepted_values(value, expected):
    assert nc.parse_time_period(value) == expected


@pytest.mark.parametrize('value', [0, 3, 'fortnight', ''])
def test_parse_time_period_rejects_bad_values(value):
    with pytest.raises(ValueError):
        nc.parse_time_period(value)


def test_parse_time_period_error_has_message():
    # A bare `raise ValueError()` here used to give no clue what was wrong.
    with pytest.raises(ValueError, match='Invalid value for range'):
        nc.parse_time_period('fortnight')


def test_currents_csv_url_includes_station_and_date():
    url = nc.currents_csv_url('NYH1928_12', pendulum.parse('2026-07-27'), 1)
    assert 'id=NYH1928_12' in url
    assert 'start_date=2026-07-27' in url
    assert 'range=1' in url
    assert url.startswith(nc.CURRENTS_CSV_URL_BASE)


def _predictions_frame():
    return pd.DataFrame({
        'Date_Time (LST_LDT)': [
            '2026-07-27 06:30 AM',
            '2026-07-27 09:45 AM',
            '2026-07-28 07:00 AM',
        ],
        'Speed (knots)': [1.5, 0.0, -1.2],
    })


def test_format_currents_table_labels_stages_and_speeds():
    text = nc.format_currents_table(_predictions_frame(), date='2026-07-27')
    lines = text.split('\n')
    assert len(lines) == 2
    assert lines[0] == '06:30 AM  max flood (1.7 mph)'
    assert lines[1] == '09:45 AM  slack'  # No speed shown for slack water


def test_format_currents_table_marks_ebb():
    text = nc.format_currents_table(_predictions_frame(), date='2026-07-28')
    assert text == '07:00 AM  max ebb (1.4 mph)'


def test_format_currents_table_empty_for_missing_date():
    assert nc.format_currents_table(_predictions_frame(), date='2026-01-01') == ''


def _predictions():
    return nc.CurrentsPredictions(_predictions_frame(), 'https://example.test/predictions')


def test_format_currents_post_single_day():
    text = nc.format_currents_post(_predictions(), nc.default_nykp_station, days=1)
    assert text.startswith('<https://example.test/predictions|NOAA current predictions at Hudson River, Pier 92')
    assert '*Monday, July 27, 2026*' in text
    assert '*Tuesday, July 28, 2026*' not in text


def test_format_currents_post_separates_multiple_days():
    # Regression: day blocks used to be concatenated with no separator, so the
    # second day's header ran onto the end of the first day's last line.
    text = nc.format_currents_post(_predictions(), nc.default_nykp_station, days=2)
    lines = text.split('\n')
    assert '*Monday, July 27, 2026*' in lines
    assert '*Tuesday, July 28, 2026*' in lines
    assert '09:45 AM  slack*Tuesday, July 28, 2026*' not in text


def test_retrieve_currents_table_leaves_no_files_behind(tmp_path, monkeypatch):
    csv_text = _predictions_frame().to_csv(index=False)
    written = []

    def fake_urlretrieve(url, path):
        written.append(path)
        with open(path, 'w') as f:
            f.write(csv_text)
        return path, {'Content-Type': 'text/csv'}

    monkeypatch.setattr(nc, 'urlretrieve', fake_urlretrieve)
    monkeypatch.chdir(tmp_path)

    predictions = nc.retrieve_currents_table('NYH1928_12', date='2026-07-27')

    assert len(predictions.table) == 3
    assert list(tmp_path.iterdir()) == []  # Nothing dropped in the working dir
    assert not any(os.path.exists(p) for p in written)  # Temp dir cleaned up


def test_retrieve_currents_table_cleans_up_on_parse_failure(tmp_path, monkeypatch):
    def fake_urlretrieve(url, path):
        with open(path, 'w') as f:
            f.write('<html>rate limited</html>')
        return path, {}

    monkeypatch.setattr(nc, 'urlretrieve', fake_urlretrieve)
    monkeypatch.setattr(pd, 'read_csv', lambda *a, **k: (_ for _ in ()).throw(ValueError('bad csv')))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError):
        nc.retrieve_currents_table('NYH1928_12', date='2026-07-27')

    assert list(tmp_path.iterdir()) == []

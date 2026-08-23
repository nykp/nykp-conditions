import os

import pandas as pd
import pendulum
import pytest

import nykp_conditions.noaa_currents as nc


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


def _table(rows):
    return pd.DataFrame(rows, columns=['Date_Time (LST_LDT)', 'Speed (knots)'])


def test_missing_speed_is_not_reported_as_slack():
    # Bug: NOAA leaves blank cells for missing readings, which parse to NaN.
    # NaN compares False against both thresholds, so the row fell through to
    # 'slack' -- telling paddlers there was no current where there was no data.
    df = _table([['2026-08-21 06:12 AM', float('nan')],
                 ['2026-08-21 09:30 AM', -1.4]])
    text = nc.format_currents_table(df, date='2026-08-21')
    assert text.splitlines()[0] == '06:12 AM  no reading'
    assert text.splitlines()[1] == '09:30 AM  max ebb (1.6 mph)'


def test_missing_speed_shows_no_mph():
    df = _table([['2026-08-21 06:12 AM', float('nan')]])
    assert 'mph' not in nc.format_currents_table(df, date='2026-08-21')


@pytest.mark.parametrize('knots, expected', [
    (1.4, 'max flood'),
    (-1.4, 'max ebb'),
    (0.0, 'slack'),
    (0.1, 'slack'),      # Threshold is exclusive
    (-0.1, 'slack'),
    (0.11, 'max flood'),
    (float('nan'), 'no reading'),
    (None, 'no reading'),
])
def test_current_stage(knots, expected):
    assert nc.current_stage(knots) == expected


def test_columns_are_matched_without_their_units():
    # rename_cols matches on a substring of the header so a units change
    # doesn't break the parse; passing the fully-qualified 'Speed (knots)'
    # defeated that.
    df = pd.DataFrame({'Date_Time (GMT)': ['2026-08-21 06:12 AM'],
                       'Speed (cm/s)': [-1.4]})
    assert '06:12 AM  max ebb' in nc.format_currents_table(df, date='2026-08-21')


def test_format_currents_post_survives_a_units_change():
    # Regression: this read 'Date_Time (LST_LDT)' off the frame directly,
    # so it raised KeyError on exactly the headers the table handled.
    df = pd.DataFrame({'Date_Time (GMT)': ['2026-08-21 06:12 AM'],
                       'Speed (cm/s)': [-1.4]})
    predictions = nc.CurrentsPredictions(df, 'http://link')
    assert '06:12 AM  max ebb' in nc.format_currents_post(
        predictions, nc.default_nykp_station
    )


def test_normalize_does_not_mutate_the_caller_s_frame():
    df = _table([['2026-08-21 06:12 AM', -1.4]])
    nc.normalize_currents_table(df)
    assert list(df.columns) == ['Date_Time (LST_LDT)', 'Speed (knots)']


# ------------------------------------------------------- station resolution

@pytest.fixture
def captured_url(monkeypatch, tmp_path):
    """Capture the URL retrieve_currents_table would download."""
    urls = []

    def fake_urlretrieve(url, path):
        urls.append(url)
        with open(path, 'w') as f:
            f.write('Date_Time (LST_LDT),Speed (knots)\n2026-08-21 06:12 AM,-1.4\n')
        return path, {}

    monkeypatch.setattr(nc, 'urlretrieve', fake_urlretrieve)
    return urls


def test_station_name_from_the_library_resolves_to_its_id(captured_url):
    nc.retrieve_currents_table('Hudson_River_Pier_92', date='2026-08-21')
    assert 'id=NYH1928_12' in captured_url[0]


def test_unknown_station_string_is_used_as_a_raw_id(captured_url):
    nc.retrieve_currents_table('NYH1927_13', date='2026-08-21')
    assert 'id=NYH1927_13' in captured_url[0]


def test_station_object_resolves_to_its_id(captured_url):
    nc.retrieve_currents_table(nc.StationLibrary.Hudson_River_Entrance, date='2026-08-21')
    assert 'id=NYH1927_13' in captured_url[0]


def test_link_points_at_the_stations_page(captured_url):
    predictions = nc.retrieve_currents_table('NYH1928_12', date='2026-08-21')
    assert predictions.link.endswith('id=NYH1928_12')


def test_station_repr_includes_depth():
    assert repr(nc.StationLibrary.Hudson_River_Pier_92) == 'Hudson River, Pier 92 (NYH1928_12), 6 feet'
    assert repr(nc.Station('Nameless', 'X1')) == 'Nameless (X1)'


# -------------------------------------------------------------- post_currents

def test_post_currents_uses_the_default_station(monkeypatch):
    posted = []
    df = pd.DataFrame({'Date_Time (LST_LDT)': ['2026-08-21 06:12 AM'], 'Speed (knots)': [-1.4]})
    monkeypatch.setattr(nc, 'retrieve_currents_table',
                        lambda **kw: nc.CurrentsPredictions(df, 'http://link'))
    monkeypatch.setattr(nc, 'post_message', lambda text, **kw: posted.append(text))

    nc.post_currents('test-channel')
    assert nc.default_nykp_station.name in posted[0]


def test_post_currents_does_not_upload_without_a_plot(monkeypatch):
    df = pd.DataFrame({'Date_Time (LST_LDT)': ['2026-08-21 06:12 AM'], 'Speed (knots)': [-1.4]})
    monkeypatch.setattr(nc, 'retrieve_currents_table',
                        lambda **kw: nc.CurrentsPredictions(df, 'http://link'))
    monkeypatch.setattr(nc, 'post_message', lambda text, **kw: None)
    monkeypatch.setattr(nc, 'post_file',
                        lambda *a, **k: pytest.fail('no plot path was set'))
    nc.post_currents('test-channel')


def test_post_currents_uploads_a_plot_when_one_exists(monkeypatch):
    df = pd.DataFrame({'Date_Time (LST_LDT)': ['2026-08-21 06:12 AM'], 'Speed (knots)': [-1.4]})
    uploads = []
    monkeypatch.setattr(nc, 'retrieve_currents_table',
                        lambda **kw: nc.CurrentsPredictions(df, 'http://link', '/tmp/plot.png'))
    monkeypatch.setattr(nc, 'post_message', lambda text, **kw: None)
    monkeypatch.setattr(nc, 'post_file', lambda path, **k: uploads.append(path))

    nc.post_currents('test-channel')
    assert uploads == ['/tmp/plot.png']


def test_format_currents_table_defaults_to_today():
    today = pendulum.today().format('YYYY-MM-DD')
    df = _table([[f'{today} 06:12 AM', -1.4], ['1999-01-01 09:30 AM', 1.4]])
    assert nc.format_currents_table(df) == '06:12 AM  max ebb (1.6 mph)'


def test_predictions_repr_names_the_class():
    predictions = nc.CurrentsPredictions(_table([['2026-08-21 06:12 AM', -1.4]]), 'http://link')
    assert repr(predictions).startswith('CurrentsPredictions')


@pytest.mark.parametrize('value, expected', [('W', 2), ('1W', 2), ('48H', 1), ('2D', 1)])
def test_parse_time_period_ignores_case(value, expected):
    assert nc.parse_time_period(value) == expected

import io
import json
import os

import pandas as pd
import pytest

import water_temps as wt


PAYLOAD = {
    'metadata': {'id': '8518750', 'name': 'The Battery', 'lat': '40.7006', 'lon': '-74.0142'},
    'data': [
        {'t': '2026-07-27 00:00', 'v': '70.1'},
        {'t': '2026-07-27 01:00', 'v': '70.3'},
        {'t': '2026-07-27 02:00', 'v': '70.2'},
    ],
}


@pytest.fixture
def stub_api(monkeypatch):
    monkeypatch.setattr(wt, 'urlopen', lambda url: io.StringIO(json.dumps(PAYLOAD)))


def test_get_water_temps_parses_station_and_series(stub_api):
    station, temps = wt.get_water_temps()
    assert station.id == '8518750'
    assert station.name == 'The Battery'
    assert len(temps) == 3
    assert isinstance(temps.index, pd.DatetimeIndex)
    assert temps.index.tz is not None  # Timestamps are UTC-aware


def test_get_water_temps_writes_raw_json_when_path_given(tmp_path, monkeypatch):
    def fake_urlretrieve(url, path):
        with open(path, 'w') as f:
            json.dump(PAYLOAD, f)
        return path, {}

    monkeypatch.setattr(wt, 'urlretrieve', fake_urlretrieve)
    target = tmp_path / 'nested' / 'raw.json'
    station, temps = wt.get_water_temps(path=str(target))
    assert target.exists()
    assert len(temps) == 3


def test_get_water_temps_accepts_bare_filename(tmp_path, monkeypatch):
    # Regression: os.path.dirname('raw.json') is '', which makedirs rejects.
    def fake_urlretrieve(url, path):
        with open(path, 'w') as f:
            json.dump(PAYLOAD, f)
        return path, {}

    monkeypatch.setattr(wt, 'urlretrieve', fake_urlretrieve)
    monkeypatch.chdir(tmp_path)
    station, temps = wt.get_water_temps(path='raw.json')
    assert (tmp_path / 'raw.json').exists()


def test_plot_temps_file_writes_png(tmp_path, stub_api):
    station, temps = wt.get_water_temps()
    target = tmp_path / 'plot.png'
    path = wt.plot_temps_file(temps, station, path=str(target))
    assert path == str(target)
    assert target.stat().st_size > 0


def test_temp_range_reports_extremes_in_local_time(stub_api):
    station, temps = wt.get_water_temps()
    rng = wt.temp_range(temps)
    assert (rng.low, rng.high) == (70.1, 70.3)
    # 00:00 and 01:00 UTC on 2026-07-27, a Monday, are Sunday evening locally.
    assert str(rng.low_at) == '2026-07-26 20:00:00-04:00'
    assert str(rng.high_at) == '2026-07-26 21:00:00-04:00'


def test_temp_range_compares_numerically(monkeypatch):
    """Regression: NOAA sends strings, where '9.5' sorts above '70.1'."""
    payload = {
        'metadata': PAYLOAD['metadata'],
        'data': [{'t': '2026-01-27 00:00', 'v': '39.5'}, {'t': '2026-01-27 01:00', 'v': '40.1'}],
    }
    monkeypatch.setattr(wt, 'urlopen', lambda url: io.StringIO(json.dumps(payload)))
    station, temps = wt.get_water_temps()
    rng = wt.temp_range(temps)
    assert (rng.low, rng.high) == (39.5, 40.1)


def test_temp_range_raises_on_empty_series():
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz='UTC'))
    with pytest.raises(ValueError, match='No water temperature observations'):
        wt.temp_range(empty)


def test_format_temps_post_reports_range_and_times(stub_api):
    station, temps = wt.get_water_temps()
    post = wt.format_temps_post(station, wt.temp_range(temps))
    assert 'The Battery' in post
    assert '*Low 70.1°F* at 8pm Sun' in post
    assert '*High 70.3°F* at 9pm Sun' in post


def test_post_water_temps_posts_text_without_a_plot_by_default(monkeypatch, stub_api):
    posted = {}
    monkeypatch.setattr(wt, 'post_message', lambda text, **kwargs: posted.update(text=text))
    monkeypatch.setattr(wt, 'post_file', lambda *a, **kw: pytest.fail('should not upload a plot'))

    wt.post_water_temps('test-channel')

    assert '*Low 70.1°F*' in posted['text']
    assert '*High 70.3°F*' in posted['text']


def test_post_water_temps_uploads_existing_file_then_cleans_up(monkeypatch, stub_api):
    uploaded = {}

    def fake_post_file(path, title, channel, comment=None):
        uploaded['path'] = path
        uploaded['existed_during_upload'] = os.path.exists(path)
        return {}

    monkeypatch.setattr(wt, 'post_file', fake_post_file)
    wt.post_water_temps('test-channel', plot=True)

    assert uploaded['existed_during_upload'] is True
    assert not os.path.exists(uploaded['path'])


def _utc_series(periods=36, start='2026-08-21T00:00Z'):
    index = pd.date_range(start, periods=periods, freq='h')
    return pd.Series(range(70, 70 + periods), index=index, dtype=float)


def test_plot_title_uses_local_time(tmp_path, monkeypatch):
    # Bug: the series is UTC-indexed and matplotlib renders tz-aware timestamps
    # in UTC unless told otherwise, so the plot disagreed with the message
    # text -- which reports local time via temp_range -- by four hours.
    station = wt.Station(id='8518750', name='The Battery', lat=40.7, lon=-74.0)
    titles = []
    monkeypatch.setattr(wt.plt, 'title', lambda t: titles.append(t))

    wt.plot_temps_file(_utc_series(), station, path=str(tmp_path / 'p.png'))

    # 00:00Z on 8/21 is 20:00 on 8/20 in New York.
    assert titles[0].startswith('20:00:00 08/20/2026')


def test_plot_axis_formatter_carries_the_local_zone(tmp_path, monkeypatch):
    station = wt.Station(id='8518750', name='The Battery', lat=40.7, lon=-74.0)
    formatters = []
    real = wt.DateFormatter
    monkeypatch.setattr(wt, 'DateFormatter',
                        lambda fmt, tz=None: formatters.append(tz) or real(fmt, tz=tz))

    wt.plot_temps_file(_utc_series(), station, path=str(tmp_path / 'p.png'))

    assert formatters and str(formatters[0]) == 'America/New_York'


def test_plot_and_post_text_agree_on_the_time(tmp_path, monkeypatch):
    # The two used different clocks for the same instant.
    station = wt.Station(id='8518750', name='The Battery', lat=40.7, lon=-74.0)
    temps = _utc_series()
    titles = []
    monkeypatch.setattr(wt.plt, 'title', lambda t: titles.append(t))
    wt.plot_temps_file(temps, station, path=str(tmp_path / 'p.png'))

    rng = wt.temp_range(temps)
    assert rng.low_at.strftime('%H:%M:%S %m/%d/%Y') == titles[0].split(' - ')[0]


def test_plot_temps_file_uses_a_temp_file_without_a_path():
    station = wt.Station(id='8518750', name='The Battery', lat=40.7, lon=-74.0)
    path = wt.plot_temps_file(_utc_series(), station)
    try:
        assert os.path.basename(path).startswith('water_temps_8518750_')
        assert os.path.getsize(path) > 0
    finally:
        os.unlink(path)

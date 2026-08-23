import io
import os
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

import nykp_conditions.nws_forecast as nf
from nykp_conditions.utils.geo import LatLon


FORECAST_PAGE = """
<html><body>
  <img src="images/logo.png"/>
  <img src="meteograms/Plotter.php?lat=40.7&lon=-74.0&wfo=OKX"/>
</body></html>
"""

TEXT_PAGE = """
<html><body>
  <table><tr><td>New York, NY</td><td>ignored</td><td>ignored</td><td>40.71N 74.01W</td></tr></table>
  <table>
    <tr><td>Today</td><td>Sunny, with a high near 85.</td></tr>
    <tr><td>Tonight</td><td>Clear, with a low around 70.</td></tr>
    <tr><td>Tuesday</td><td>Showers likely.</td></tr>
  </table>
</body></html>
"""


def _stub_urlopen(monkeypatch, html):
    monkeypatch.setattr(nf, 'urlopen', lambda url: io.StringIO(html))


def _fake_urlretrieve(url, path):
    """Stand-in for urlretrieve that writes a placeholder file."""
    with open(path, 'wb') as f:
        f.write(b'PNG')
    return path, {}


def test_is_forecast_img_tag():
    soup = BeautifulSoup(FORECAST_PAGE, 'html.parser')
    tags = soup.find_all('img')
    assert [nf._is_forecast_img_tag(t) for t in tags] == [False, True]


def test_is_forecast_img_tag_handles_missing_src():
    tag = BeautifulSoup('<img alt="no src"/>', 'html.parser').find('img')
    assert nf._is_forecast_img_tag(tag) is False


def test_find_forecast_img_url(monkeypatch):
    _stub_urlopen(monkeypatch, FORECAST_PAGE)
    url = nf.find_forecast_img_url()
    assert url == 'https://forecast.weather.gov/meteograms/Plotter.php?lat=40.7&lon=-74.0&wfo=OKX'


def test_find_forecast_img_url_raises_when_absent(monkeypatch):
    _stub_urlopen(monkeypatch, '<html><body><img src="images/logo.png"/></body></html>')
    with pytest.raises(RuntimeError, match='Could not find forecast image'):
        nf.find_forecast_img_url()


def test_find_forecast_img_url_raises_when_ambiguous(monkeypatch):
    _stub_urlopen(monkeypatch, '<html><body>'
                               '<img src="meteograms/Plotter.php?a=1"/>'
                               '<img src="meteograms/Plotter.php?a=2"/>'
                               '</body></html>')
    with pytest.raises(RuntimeError, match='Multiple candidate'):
        nf.find_forecast_img_url()


def test_find_forecast_img_url_accepts_tuple_lat_lon(monkeypatch):
    _stub_urlopen(monkeypatch, FORECAST_PAGE)
    assert nf.find_forecast_img_url((40.7, -74.0)).startswith('https://forecast.weather.gov/meteograms/')


def test_parse_forecast_text():
    forecast = nf.parse_forecast_text(BeautifulSoup(TEXT_PAGE, 'html.parser'))
    assert forecast.title == 'New York, NY, 40.71N 74.01W'
    assert forecast.forecast == 'Today Sunny, with a high near 85.\n\nTonight Clear, with a low around 70.'


def test_parse_forecast_text_respects_num_periods():
    forecast = nf.parse_forecast_text(BeautifulSoup(TEXT_PAGE, 'html.parser'), num_periods=3)
    assert 'Tuesday Showers likely.' in forecast.forecast


def test_parse_forecast_text_raises_on_unexpected_layout():
    soup = BeautifulSoup('<html><body><table><tr><td>only one</td></tr></table></body></html>', 'html.parser')
    with pytest.raises(RuntimeError, match='Expected 2 tables'):
        nf.parse_forecast_text(soup)


def test_forecast_img_filename_is_timestamped():
    now = datetime(2026, 7, 27, 12, 30, 0, tzinfo=timezone.utc)
    name = nf.forecast_img_filename(LatLon(40.7143, -74.006), now=now)
    assert name == 'nws_forecast_40.7143_-74.006_20260727T123000.png'


def test_save_forecast_plot_writes_to_given_path(tmp_path, monkeypatch):
    monkeypatch.setattr(nf, 'find_forecast_img_url', lambda lat_lon: 'https://example.test/plot.png')
    monkeypatch.setattr(nf, 'urlretrieve', _fake_urlretrieve)
    target = tmp_path / 'nested' / 'plot.png'
    path = nf.save_forecast_plot(path=str(target))
    assert path == str(target)
    assert target.read_bytes() == b'PNG'


def test_post_forecast_removes_temp_file_after_upload(monkeypatch):
    """The image must exist during the upload and be gone afterwards."""
    monkeypatch.setattr(nf, 'find_forecast_img_url', lambda lat_lon: 'https://example.test/plot.png')
    monkeypatch.setattr(nf, 'get_forecast_text', lambda lat_lon: nf.ForecastText('Title', 'Body'))
    monkeypatch.setattr(nf, 'urlretrieve', _fake_urlretrieve)

    uploaded = {}

    def fake_post_file(path, title, channel, comment=None):
        uploaded['path'] = path
        uploaded['existed_during_upload'] = os.path.exists(path)
        uploaded['comment'] = comment
        return {}

    monkeypatch.setattr(nf, 'post_file', fake_post_file)

    nf.post_forecast('test-channel')

    assert uploaded['existed_during_upload'] is True
    assert not os.path.exists(uploaded['path'])
    assert uploaded['comment'] == '*Title*\n\nBody'


def test_post_forecast_text_only_does_not_touch_files(monkeypatch):
    # Regression: with plot=False the old cleanup appended None to its path
    # list and then blew up in os.remove(None).
    monkeypatch.setattr(nf, 'get_forecast_text', lambda lat_lon: nf.ForecastText('Title', 'Body'))
    posted = {}
    monkeypatch.setattr(nf, 'post_message', lambda msg, channel: posted.update(msg=msg, channel=channel))

    nf.post_forecast('test-channel', plot=False)

    assert posted['msg'] == '*Title*\n\nBody'
    assert posted['channel'] == 'test-channel'


def test_post_forecast_requires_something_to_post():
    with pytest.raises(ValueError, match='at least one'):
        nf.post_forecast('test-channel', text=False, plot=False)


def test_post_forecast_keep_writes_to_cache_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(nf, 'find_forecast_img_url', lambda lat_lon: 'https://example.test/plot.png')
    monkeypatch.setattr(nf, 'urlretrieve', _fake_urlretrieve)
    monkeypatch.setattr(nf, 'post_file', lambda path, title, channel, comment=None: {})

    nf.post_forecast('test-channel', text=False, keep=True)

    kept = list((tmp_path / nf.CACHE_DIR).iterdir())
    assert len(kept) == 1
    assert kept[0].name.startswith('nws_forecast_')


def test_get_forecast_text_requests_the_locations_page(monkeypatch):
    urls = []

    def fake_urlopen(url):
        urls.append(url)
        return io.StringIO(TEXT_PAGE)

    monkeypatch.setattr(nf, 'urlopen', fake_urlopen)
    forecast = nf.get_forecast_text(nf.LatLon(40.7143, -74.006))

    assert 'lat=40.7143' in urls[0] and 'lon=-74.006' in urls[0]
    assert 'FcstType=text' in urls[0]
    assert forecast.title == 'New York, NY, 40.71N 74.01W'


def test_get_forecast_text_accepts_a_tuple(monkeypatch):
    _stub_urlopen(monkeypatch, TEXT_PAGE)
    assert nf.get_forecast_text((40.7143, -74.006)).forecast.startswith('Today')


def test_save_forecast_plot_uses_a_temp_file_without_a_path(monkeypatch):
    monkeypatch.setattr(nf, 'find_forecast_img_url', lambda lat_lon: 'http://x/plot.png')
    written = []

    def fake_urlretrieve(url, path):
        written.append(path)
        with open(path, 'wb') as f:
            f.write(b'\x89PNG')
        return path, {}

    monkeypatch.setattr(nf, 'urlretrieve', fake_urlretrieve)
    path = nf.save_forecast_plot()
    try:
        assert path == written[0]
        assert os.path.basename(path).startswith('nws_forecast_')
        assert os.path.getsize(path) > 0
    finally:
        os.unlink(path)


def test_parse_forecast_text_raises_when_no_periods_are_present():
    page = ('<html><body><table><tr><td>New York</td><td>a</td><td>b</td>'
            '<td>40.71N 74.01W</td></tr></table><table></table></body></html>')
    with pytest.raises(RuntimeError, match='No forecast periods'):
        nf.parse_forecast_text(BeautifulSoup(page, 'html.parser'))

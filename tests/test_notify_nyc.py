from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

import notify_nyc as nn


def test_find_links_picks_out_urls():
    summary = 'See https://example.test/a and http://example.test/b for details. Not a link: example.test'
    assert nn.find_links(summary) == ['https://example.test/a', 'http://example.test/b']


def test_find_links_none_present():
    assert nn.find_links('No links here.') == []


def test_alert_published_uses_real_eastern_offset():
    # Regression: this used to use pytz, where `.replace(tzinfo=zone)` gives
    # New York's 1883 LMT offset (-4:56) rather than EDT. zoneinfo resolves the
    # offset from the date, so the naive-datetime path is now correct by default.
    alert = nn.NotifyAlert(title='Waterbody Advisory', published='07/04/2026 12:00:00', summary='')
    assert alert.published.utcoffset() == timedelta(hours=-4)  # EDT
    assert alert.published.tzname() == 'EDT'


def test_alert_published_uses_standard_time_in_winter():
    alert = nn.NotifyAlert(title='Waterbody Advisory', published='01/04/2026 12:00:00', summary='')
    assert alert.published.utcoffset() == timedelta(hours=-5)  # EST
    assert alert.published.tzname() == 'EST'


def test_alert_published_naive_datetime_is_localized():
    alert = nn.NotifyAlert(title='x', published=datetime(2026, 7, 4, 12, 0, 0), summary='')
    assert alert.published.tzinfo is not None
    assert alert.published.utcoffset() == timedelta(hours=-4)


def test_alert_parse_ignores_extra_feed_fields():
    entry = {
        'title': 'Waterbody Advisory',
        'published': '07/04/2026 12:00:00',
        'summary': 'CSO event',
        'id': 'urn:uuid:1234',
        'links': [],
    }
    alert = nn.NotifyAlert.parse(entry)
    assert alert.title == 'Waterbody Advisory'
    assert alert.summary == 'CSO event'


def _entry(title, published):
    return {'title': title, 'published': published, 'summary': 'summary text'}


@pytest.fixture
def fake_feed(monkeypatch):
    """Stub feedparser so advisory filtering can be tested without network."""
    def _install(entries):
        monkeypatch.setattr(nn.feedparser, 'parse', lambda url: {'entries': entries})
    return _install


def test_get_waterbody_advisories_filters_by_title(fake_feed):
    fake_feed([
        _entry('Waterbody Advisory for the Hudson', '07/04/2026 12:00:00'),
        _entry('Traffic Advisory', '07/04/2026 12:00:00'),
    ])
    advisories = nn.get_waterbody_advisories()
    assert [a.title for a in advisories] == ['Waterbody Advisory for the Hudson']


def test_get_waterbody_advisories_filters_by_window(fake_feed):
    fake_feed([
        _entry('Waterbody Advisory old', '07/01/2026 12:00:00'),
        _entry('Waterbody Advisory recent', '07/04/2026 12:00:00'),
    ])
    tz = ZoneInfo('America/New_York')
    start = datetime(2026, 7, 3, tzinfo=tz)
    end = datetime(2026, 7, 5, tzinfo=tz)
    advisories = nn.get_waterbody_advisories(start_dt=start, end_dt=end)
    assert [a.title for a in advisories] == ['Waterbody Advisory recent']


def test_get_waterbody_advisories_accepts_naive_bounds(fake_feed):
    # Naive bounds used to raise TypeError when compared against aware alerts.
    fake_feed([_entry('Waterbody Advisory recent', '07/04/2026 12:00:00')])
    advisories = nn.get_waterbody_advisories(
        start_dt=datetime(2026, 7, 3), end_dt=datetime(2026, 7, 5)
    )
    assert len(advisories) == 1


def test_get_waterbody_advisories_empty_feed(fake_feed):
    fake_feed([])
    assert nn.get_waterbody_advisories() == []

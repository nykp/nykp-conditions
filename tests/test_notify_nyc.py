from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

import nykp_conditions.notify_nyc as nn


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


# The feed carries every NYC alert, so a malformed entry we don't even want
# used to take down the whole waterbody section.

def test_malformed_entry_does_not_break_the_feed(fake_feed):
    fake_feed([
        {'published': '07/04/2026 12:00:00', 'summary': 'a traffic notice'},  # No title
        _entry('Waterbody Advisory real', '07/04/2026 12:00:00'),
    ])
    advisories = nn.get_waterbody_advisories()
    assert [a.title for a in advisories] == ['Waterbody Advisory real']


def test_entry_without_a_summary_still_parses(fake_feed):
    fake_feed([{'title': 'Waterbody Advisory', 'published': '07/04/2026 12:00:00'}])
    advisory = nn.get_waterbody_advisories()[0]
    assert advisory.summary == ''
    assert nn.find_links(advisory.summary) == []


def test_undated_entry_is_excluded_from_a_window(fake_feed):
    fake_feed([{'title': 'Waterbody Advisory', 'summary': 'x'}])
    tz = ZoneInfo('America/New_York')
    assert nn.get_waterbody_advisories(
        start_dt=datetime(2026, 7, 1, tzinfo=tz), end_dt=datetime(2026, 7, 10, tzinfo=tz)
    ) == []


def test_undated_entry_is_kept_when_no_window_is_requested(fake_feed):
    fake_feed([{'title': 'Waterbody Advisory', 'summary': 'x'}])
    assert len(nn.get_waterbody_advisories()) == 1


# ------------------------------------------------------- post_waterbody_advisories

@pytest.fixture
def captured_posts(monkeypatch):
    posts = []
    monkeypatch.setattr(nn, 'post_text_attachment',
                        lambda **kwargs: posts.append(kwargs))
    return posts


def test_post_sends_one_message_per_advisory(fake_feed, captured_posts):
    fake_feed([
        _entry('Waterbody Advisory one', '07/04/2026 12:00:00'),
        _entry('Waterbody Advisory two', '07/04/2026 13:00:00'),
        _entry('Traffic Advisory', '07/04/2026 12:00:00'),
    ])
    nn.post_waterbody_advisories('test-channel', start_time='07/01/2026', end_time='07/10/2026')

    assert len(captured_posts) == 2
    assert all(p['channel'] == 'test-channel' for p in captured_posts)
    assert 'Waterbody Advisory one' in captured_posts[0]['pretext']


def test_post_appends_links_from_the_summary(fake_feed, captured_posts):
    fake_feed([{'title': 'Waterbody Advisory', 'published': '07/04/2026 12:00:00',
                'summary': 'Details at https://nyc.gov/dep and https://nyc.gov/health'}])
    nn.post_waterbody_advisories('c', start_time='07/01/2026', end_time='07/10/2026')

    pretext = captured_posts[0]['pretext']
    assert 'Links:' in pretext
    assert 'https://nyc.gov/dep' in pretext and 'https://nyc.gov/health' in pretext


def test_post_omits_the_links_block_when_there_are_none(fake_feed, captured_posts):
    fake_feed([_entry('Waterbody Advisory', '07/04/2026 12:00:00')])
    nn.post_waterbody_advisories('c', start_time='07/01/2026', end_time='07/10/2026')
    assert 'Links:' not in captured_posts[0]['pretext']


def test_post_defaults_to_the_last_day(fake_feed, captured_posts):
    now = datetime.now(tz=ZoneInfo('America/New_York'))
    recent = now - timedelta(hours=2)
    old = now - timedelta(days=5)
    fake_feed([
        _entry('Waterbody Advisory recent', recent.strftime(nn.NOTIFY_DATE_FMT)),
        _entry('Waterbody Advisory old', old.strftime(nn.NOTIFY_DATE_FMT)),
    ])
    nn.post_waterbody_advisories('c')

    assert len(captured_posts) == 1
    assert 'recent' in captured_posts[0]['pretext']


def test_post_days_argument_widens_the_window(fake_feed, captured_posts):
    now = datetime.now(tz=ZoneInfo('America/New_York'))
    fake_feed([
        _entry('Waterbody Advisory recent', (now - timedelta(hours=2)).strftime(nn.NOTIFY_DATE_FMT)),
        _entry('Waterbody Advisory old', (now - timedelta(days=5)).strftime(nn.NOTIFY_DATE_FMT)),
    ])
    nn.post_waterbody_advisories('c', days=7)
    assert len(captured_posts) == 2


def test_post_says_nothing_when_there_are_no_advisories(fake_feed, captured_posts):
    fake_feed([_entry('Traffic Advisory', '07/04/2026 12:00:00')])
    nn.post_waterbody_advisories('c', start_time='07/01/2026', end_time='07/10/2026')
    assert captured_posts == []


def test_published_falls_back_to_pendulum_for_other_formats():
    # The feed's usual format is '%m/%d/%Y %H:%M:%S'; anything else goes
    # through pendulum rather than failing the whole entry.
    alert = nn.NotifyAlert(title='x', published='2026-07-04T12:00:00', summary='')
    assert alert.published.year == 2026
    assert alert.published.utcoffset() == timedelta(hours=-4)


def test_end_bound_alone_excludes_later_alerts(fake_feed):
    fake_feed([
        _entry('Waterbody Advisory early', '07/01/2026 12:00:00'),
        _entry('Waterbody Advisory late', '07/09/2026 12:00:00'),
    ])
    advisories = nn.get_waterbody_advisories(end_dt=datetime(2026, 7, 5))
    assert [a.title for a in advisories] == ['Waterbody Advisory early']


def test_window_bounds_are_read_as_local_time(fake_feed, captured_posts):
    # Same bug as above on the CLI's --start/--end: pendulum read a naive
    # bound as UTC, sliding the window four hours off the feed's clock.
    fake_feed([_entry('Waterbody Advisory', '07/04/2026 22:00:00')])
    nn.post_waterbody_advisories('c', start_time='2026-07-04T20:00:00',
                                 end_time='2026-07-04T23:00:00')
    assert len(captured_posts) == 1

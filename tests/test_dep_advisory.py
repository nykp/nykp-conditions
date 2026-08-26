from datetime import datetime, timedelta
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import pytest
from zoneinfo import ZoneInfo

import nykp_conditions.dep_advisory as dep

NY = ZoneInfo('America/New_York')

LAYER_URL = 'https://services.arcgis.test/services/waterbody_wq/FeatureServer/0/'
PROFILE = {'environment': 'Production', 'wq.arcgis.feature_layer.url': LAYER_URL}


def _record(name, duration, occurred_on='2026-08-21T08:00:00', wb_id=21,
            water_class='SB', gauge='NR', message=None):
    return {
        'occurredOn': occurred_on,
        'waterbody': {'id': wb_id, 'name': name, 'class': water_class, 'rainGauge': gauge},
        'duration': duration,
        'message': message or ('No Advisory' if not duration else 'On Advisory'),
        'volume': 0.0,
    }


def _feature(fid, rings, geometry_type='Polygon'):
    coordinates = rings if geometry_type == 'Polygon' else [[ring] for ring in rings]
    return {'properties': {'FID': fid},
            'geometry': {'type': geometry_type, 'coordinates': coordinates}}


SQUARE = [[-74.0, 40.7], [-74.0, 40.8], [-73.9, 40.8], [-73.9, 40.7], [-74.0, 40.7]]


@pytest.fixture
def fake_api(monkeypatch):
    """Stub every HTTP call so nothing here touches the network."""
    def _install(responses: dict):
        responses = dict(responses)
        # `get_complete_advisories` checks the advisory list against the map
        # layer's waterbody count. Default that count to however many records
        # this fake serves, so a test about posting doesn't wander into the
        # warm-up retry path -- and its sleeps -- to get there.
        if not any('profile' in key for key in responses):
            responses['/api/profile'] = PROFILE
        if not any('returnCountOnly' in key for key in responses):
            served = next((v for k, v in responses.items()
                           if 'advisory' in k and isinstance(v, list)), None)
            if served is not None:
                responses['returnCountOnly'] = {'count': len(served)}

        def _get_json(url, **params):
            # Match against the query string too, so the count request is
            # distinguishable from the polygon request -- both hit `/query`.
            target = f"{url}?{urlencode(params)}" if params else url
            # Longest key first, so a specific key wins over a general one.
            for key in sorted(responses, key=len, reverse=True):
                if key in target:
                    return responses[key]
            raise AssertionError(f"unexpected request to {url} {params}")
        monkeypatch.setattr(dep, '_get_json', _get_json)
    return _install


def test_advisory_timestamps_are_local_not_utc():
    # The API sends naive timestamps that are already New York local time.
    # Reading them as UTC would slide the whole advisory window by four hours.
    advisory = dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0))
    assert advisory.occurred_on.utcoffset() == timedelta(hours=-4)  # EDT
    assert advisory.occurred_on == datetime(2026, 8, 21, 8, tzinfo=NY)


def test_advisory_timestamps_use_standard_time_in_winter():
    advisory = dep.WaterbodyAdvisory.parse(
        _record('Hudson River (Manhattan)', 4.0, occurred_on='2026-01-21T08:00:00')
    )
    assert advisory.occurred_on.utcoffset() == timedelta(hours=-5)  # EST


def test_until_matches_the_apis_own_message():
    # Live check on 2026-08-21: occurredOn 08:00 with duration 16 came with a
    # message of "On Advisory until 8/22/2026 12:00:00 AM".
    advisory = dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0))
    assert advisory.until == datetime(2026, 8, 22, 0, 0, tzinfo=NY)


def test_zero_duration_is_no_advisory():
    advisory = dep.WaterbodyAdvisory.parse(_record('Little Neck Bay', 0.0))
    assert not advisory.active
    assert advisory.until == advisory.occurred_on


def test_get_advisories_and_find_by_name(fake_api):
    fake_api({'/advisory': [
        _record('Alley Creek', 0.0, wb_id=12),
        _record('Hudson River (Manhattan)', 16.0),
    ]})
    advisories = dep.get_advisories()
    assert len(advisories) == 2
    found = dep.find_advisory(advisories, 'Hudson River (Manhattan)')
    assert found.active and found.duration_hours == 16.0
    assert dep.find_advisory(advisories, 'Gowanus Canal, Inner') is None


def test_layer_url_comes_from_the_dashboard_profile(fake_api):
    fake_api({'/profile': PROFILE})
    assert dep.get_layer_url() == LAYER_URL


def test_layer_url_raises_when_the_profile_key_is_gone(fake_api):
    fake_api({'/profile': {'environment': 'Production'}})
    with pytest.raises(RuntimeError, match='feature_layer.url'):
        dep.get_layer_url()


def test_shapes_are_keyed_by_fid(fake_api):
    fake_api({'/profile': PROFILE, '/query': {'features': [_feature(21, [SQUARE])]}})
    shapes = dep.get_waterbody_shapes()
    assert list(shapes) == [21]
    assert np.allclose(shapes[21][0], SQUARE)


def test_multipolygon_rings_are_flattened(fake_api):
    # A waterbody that wraps an island comes back as a MultiPolygon; every ring
    # has to be drawn, not just the first polygon's.
    other = [[p[0] + 0.5, p[1]] for p in SQUARE]
    fake_api({'/profile': PROFILE,
              '/query': {'features': [_feature(7, [SQUARE, other], 'MultiPolygon')]}})
    shapes = dep.get_waterbody_shapes()
    assert len(shapes[7]) == 2


def test_shapes_drop_any_z_coordinate(fake_api):
    ring_3d = [[x, y, 0.0] for x, y in SQUARE]
    fake_api({'/profile': PROFILE, '/query': {'features': [_feature(21, [ring_3d])]}})
    assert dep.get_waterbody_shapes()[21][0].shape == (5, 2)


def test_map_is_written_and_counts_advisories(tmp_path):
    advisories = [
        dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0)),
        dep.WaterbodyAdvisory.parse(_record('Alley Creek', 0.0, wb_id=12)),
    ]
    shapes = {21: [np.array(SQUARE)], 12: [np.array(SQUARE) + 0.05]}
    path = dep.plot_advisory_map(advisories, shapes, path=str(tmp_path / 'map.png'))
    assert path == str(tmp_path / 'map.png')
    assert (tmp_path / 'map.png').stat().st_size > 0


def test_map_tolerates_a_waterbody_with_no_shape(tmp_path):
    # If DEP adds a waterbody to the API before the layer, the map should still
    # draw the ones it does have rather than blowing up the whole post.
    advisories = [
        dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0)),
        dep.WaterbodyAdvisory.parse(_record('Brand New Creek', 5.0, wb_id=99)),
    ]
    dep.plot_advisory_map(advisories, {21: [np.array(SQUARE)]}, path=str(tmp_path / 'map.png'))
    assert (tmp_path / 'map.png').stat().st_size > 0


def test_map_without_a_highlight(tmp_path):
    advisories = [dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0))]
    dep.plot_advisory_map(advisories, {21: [np.array(SQUARE)]},
                          highlight=None, path=str(tmp_path / 'map.png'))
    assert (tmp_path / 'map.png').stat().st_size > 0


def test_format_post_when_active():
    advisories = [
        dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0)),
        dep.WaterbodyAdvisory.parse(_record('Alley Creek', 0.0, wb_id=12)),
    ]
    text = dep.format_advisory_post(advisories[0], advisories)
    assert 'On advisory until 12am Sat' in text
    assert '1 of 2 NYC waterbodies' in text


def test_water_class_is_opt_in():
    advisory = dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0))
    assert 'Class SB' not in dep.format_advisory_post(advisory)
    assert 'Class SB' in dep.format_advisory_post(advisory, with_class=True)


"""----------------------------------------------------------------------------
The three tiers of a clear-or-not morning

Regression: the retired notify_nyc.py reposted every Notify NYC waterbody
alert, so a CSO in Flushing Creek read as an advisory on a morning the Hudson
was clear. What the post says now turns on our own waterbody's status.
----------------------------------------------------------------------------"""


def _clear_and_active(active_elsewhere=1, clear_elsewhere=0):
    """Our waterbody clear, with `active_elsewhere` others on advisory."""
    records = [_record('Hudson River (Manhattan)', 0.0)]
    records += [_record(f'Creek {i}', 6.0, wb_id=100 + i) for i in range(active_elsewhere)]
    records += [_record(f'Bay {i}', 0.0, wb_id=200 + i) for i in range(clear_elsewhere)]
    return [dep.WaterbodyAdvisory.parse(r) for r in records]


def test_format_post_when_clear_everywhere():
    advisories = [dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 0.0))]
    text = dep.format_advisory_post(advisories[0], advisories)
    assert 'No advisory for Hudson River (Manhattan).' in text
    assert 'No active advisories anywhere in the city.' in text


def test_format_post_when_clear_here_but_not_elsewhere():
    """The bug that started this: an advisory somewhere else in the city is not
    a reason to tell the channel the Hudson is off limits."""
    advisories = _clear_and_active(active_elsewhere=5, clear_elsewhere=39)
    text = dep.format_advisory_post(advisories[0], advisories)
    assert 'No advisory for Hudson River (Manhattan).' in text
    assert "5 of 45 NYC waterbodies are on advisory" in text
    assert "ours isn't one of them" in text


def test_every_tier_links_the_dashboard_portal():
    """Not nyc.gov/site/dep/water/waterbody-advisories.page, which is where the
    Notify NYC bit.ly links landed: that page has no per-waterbody status."""
    clear_everywhere = [dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 0.0))]
    clear_here = _clear_and_active(active_elsewhere=2)
    active_here = [dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0))]
    for advisories in (clear_everywhere, clear_here, active_here):
        text = dep.format_advisory_post(advisories[0], advisories)
        assert 'https://nycwaterbodyadvisory.azurewebsites.net/' in text
        assert 'nyc.gov' not in text


def test_format_post_without_the_citywide_list_stays_brief():
    """`advisories` is optional; without it there's no count to report and the
    post shouldn't invent one."""
    advisory = dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 0.0))
    text = dep.format_advisory_post(advisory)
    assert text.endswith('*No advisory for Hudson River (Manhattan).*')


def test_post_says_so_when_clear_everywhere(fake_api, monkeypatch):
    fake_api({'/advisory': [_record('Hudson River (Manhattan)', 0.0)]})
    posted = []
    monkeypatch.setattr(dep, 'post_message', lambda text, **k: posted.append(text))
    monkeypatch.setattr(dep, 'post_files',
                        lambda *a, **k: pytest.fail('no map when we are clear'))
    dep.post_waterbody_advisory('test-channel')
    assert len(posted) == 1
    assert 'No active advisories anywhere in the city.' in posted[0]


def test_post_names_the_count_when_only_elsewhere_is_on_advisory(fake_api, monkeypatch):
    fake_api({'/advisory': [_record('Hudson River (Manhattan)', 0.0),
                            _record('Flushing Creek', 12.0, wb_id=30)]})
    posted = []
    monkeypatch.setattr(dep, 'post_message', lambda text, **k: posted.append(text))
    # No map, no rainfall, no polygon fetch: a clear morning is one line and a
    # link, which is also why the fake above serves no /query or /sensors.
    monkeypatch.setattr(dep, 'post_files',
                        lambda *a, **k: pytest.fail('no map when we are clear'))
    dep.post_waterbody_advisory('test-channel')
    assert len(posted) == 1
    assert '1 of 2 NYC waterbodies are on advisory' in posted[0]


@pytest.fixture
def capture_uploads(monkeypatch):
    """Record what a post would upload, without touching Slack."""
    captured = {}

    def post_files(uploads, **kwargs):
        captured['paths'] = [path for path, _ in uploads]
        captured['titles'] = [title for _, title in uploads]
        captured['comment'] = kwargs['comment']

    monkeypatch.setattr(dep, 'post_files', post_files)
    monkeypatch.setattr(dep, 'post_message',
                        lambda *a, **k: pytest.fail('should have uploaded files'))
    return captured


ACTIVE_POST = {
    '/advisory': [_record('Hudson River (Manhattan)', 16.0)],
    '/profile': PROFILE,
    '/query': {'features': [_feature(21, [SQUARE])]},
    '/rainfall-samples': [
        {'occurredOn': '2026-08-20T22:00:00', 'precipitation': 1.24},
        {'occurredOn': '2026-08-20T21:00:00', 'precipitation': 0.31},
    ],
    '/sensors': [{'id': 'NR', 'name': 'North River'}],
}


def test_post_uploads_the_map_when_active(fake_api, capture_uploads):
    fake_api(ACTIVE_POST)
    dep.post_waterbody_advisory('test-channel')

    assert len(capture_uploads['paths']) == 1
    assert capture_uploads['paths'][0].endswith('dep_advisory_map.png')
    assert 'On advisory until' in capture_uploads['comment']


def test_rainfall_totals_post_but_the_chart_is_opt_in(fake_api, capture_uploads):
    # nws_precip.py already posts precipitation daily, so the chart is opt-in.
    # The gauge totals are a line of text and always go out.
    fake_api(ACTIVE_POST)
    dep.post_waterbody_advisory('test-channel')

    assert len(capture_uploads['paths']) == 1
    assert 'North River gauge' in capture_uploads['comment']


def test_rainfall_chart_adds_a_second_image_to_the_same_message(fake_api, capture_uploads):
    fake_api(ACTIVE_POST)
    dep.post_waterbody_advisory('test-channel', plot_rainfall=True)

    # One message carrying both images, not one message apiece.
    assert capture_uploads['paths'][0].endswith('dep_advisory_map.png')
    assert capture_uploads['paths'][1].endswith('dep_rainfall_NR.png')
    assert 'North River gauge' in capture_uploads['comment']


def test_rainfall_is_sorted_oldest_first_and_localized(fake_api):
    # The API returns samples newest-first; plotting them in that order would
    # draw the series backwards.
    fake_api({'/rainfall-samples': [
        {'occurredOn': '2026-08-21T08:00:00', 'precipitation': 0.0},
        {'occurredOn': '2026-08-21T07:00:00', 'precipitation': 0.5},
        {'occurredOn': '2026-08-21T06:00:00', 'precipitation': 0.25},
    ]})
    rainfall = dep.get_rainfall('NR')
    assert list(rainfall.index) == [datetime(2026, 8, 21, h, tzinfo=NY) for h in (6, 7, 8)]
    assert list(rainfall.values) == [0.25, 0.5, 0.0]


def test_rainfall_empty_response(fake_api):
    fake_api({'/rainfall-samples': []})
    rainfall = dep.get_rainfall('NR')
    assert rainfall.empty
    assert dep.recent_total(rainfall) == 0.0


def test_recent_total_covers_only_the_trailing_window():
    index = pd.DatetimeIndex(
        [datetime(2026, 8, 20, 8, tzinfo=NY) + timedelta(hours=i) for i in range(25)]
    )
    # 1.0 in on the oldest hour, which falls outside the trailing 24.
    rainfall = pd.Series([1.0] + [0.1] * 24, index=index)
    assert dep.recent_total(rainfall, hours=24) == pytest.approx(2.4)
    assert float(rainfall.sum()) == pytest.approx(3.4)


def test_rainfall_plot_writes_a_png(tmp_path):
    index = pd.DatetimeIndex(
        [datetime(2026, 8, 20, 18, tzinfo=NY) + timedelta(hours=i) for i in range(6)]
    )
    rainfall = pd.Series([0.5, 1.24, 0.31, 0.07, 0.37, 0.06], index=index)
    path = dep.plot_rainfall_file(rainfall, 'North River', path=str(tmp_path / 'r.png'))
    assert path == str(tmp_path / 'r.png')
    assert (tmp_path / 'r.png').stat().st_size > 0


def _hourly(values, start=datetime(2026, 8, 17, 9, tzinfo=NY)):
    index = pd.DatetimeIndex([start + timedelta(hours=i) for i in range(len(values))])
    return pd.Series([float(v) for v in values], index=index)


def test_window_hours_counts_both_endpoints():
    # Each sample is one hour's accumulation, so 96 samples cover 96 hours even
    # though only 95 hours separate the first from the last.
    assert dep.window_hours(_hourly([0.0] * 96)) == 96
    assert dep.window_hours(_hourly([0.0])) == 1
    assert dep.window_hours(pd.Series(dtype=float)) == 0


def test_window_hours_spans_a_reporting_gap():
    # A gauge that drops samples still covers the same stretch of time; counting
    # len(rainfall) would understate the period the total is drawn from.
    full = _hourly([0.0] * 96)
    with_gap = full.drop(full.index[10:20])
    assert len(with_gap) == 86
    assert dep.window_hours(with_gap) == 96


def test_totals_name_the_period_they_cover():
    # "over the plotted window" was wrong once the chart became opt-in: the
    # totals post whether or not anything is plotted.
    advisory = dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0))
    rainfall = _hourly([0.1] * 96)
    text = dep.format_advisory_post(advisory, rainfall=rainfall, gauge_name='North River')
    assert '*2.40 in* over the last 24 hours' in text
    assert '9.60 in over the last 96 hours' in text
    assert 'plotted window' not in text


def test_short_series_reports_one_total_only():
    # With 24 hours or less the two figures are the same number; printing both
    # would just look like a mistake.
    advisory = dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 16.0))
    text = dep.format_advisory_post(advisory, rainfall=_hourly([0.1] * 12),
                                    gauge_name='North River')
    assert text.endswith('*1.20 in* over the last 24 hours.')


def test_post_raises_on_unknown_waterbody(fake_api):
    fake_api({'/advisory': [_record('Hudson River (Manhattan)', 0.0)]})
    with pytest.raises(ValueError, match='No waterbody named'):
        dep.post_waterbody_advisory('test-channel', waterbody='Lake Placid')


"""----------------------------------------------------------------------------
Completeness: the dashboard warms up in stages
----------------------------------------------------------------------------"""


def _advisories(n, name='Hudson River (Manhattan)'):
    """n advisory records, the first named `name`."""
    return [dep.WaterbodyAdvisory.parse(_record(name if i == 0 else f'Water {i}', 0.0, wb_id=i))
            for i in range(n)]


def test_waterbody_count_reads_the_layer(fake_api):
    fake_api({'/api/profile': PROFILE, 'returnCountOnly': {'count': 45}})
    assert dep.get_waterbody_count() == 45


def test_waterbody_count_rejects_a_response_without_one(fake_api):
    fake_api({'/api/profile': PROFILE, 'returnCountOnly': {'error': 'nope'}})
    with pytest.raises(RuntimeError, match='no count'):
        dep.get_waterbody_count()


def test_complete_advisories_refetches_until_the_list_is_whole(monkeypatch):
    """Observed 2026-08-23: empty, then 12, then 36, then 45 records. Taking
    the first answer would report a citywide count off a partial list."""
    responses = [[], _advisories(12), _advisories(36), _advisories(45)]
    monkeypatch.setattr(dep, 'get_waterbody_count', lambda *a, **k: 45)
    monkeypatch.setattr(dep, 'get_advisories', lambda **k: responses.pop(0))

    result = dep.get_complete_advisories(sleep=lambda _: None)

    assert len(result) == 45
    assert responses == []  # Took all four attempts


def test_complete_advisories_returns_the_first_whole_response(monkeypatch):
    calls = []
    monkeypatch.setattr(dep, 'get_waterbody_count', lambda *a, **k: 45)

    def _get(**kwargs):
        calls.append(1)
        return _advisories(45)

    monkeypatch.setattr(dep, 'get_advisories', _get)
    assert len(dep.get_complete_advisories(sleep=lambda _: None)) == 45
    assert len(calls) == 1  # No needless waiting when the first read is good


def test_complete_advisories_accepts_more_records_than_expected(monkeypatch):
    """DEP can add a waterbody before the map layer catches up; that's not a
    partial response."""
    monkeypatch.setattr(dep, 'get_waterbody_count', lambda *a, **k: 45)
    monkeypatch.setattr(dep, 'get_advisories', lambda **k: _advisories(46))
    assert len(dep.get_complete_advisories(sleep=lambda _: None)) == 46


def test_complete_advisories_waits_between_attempts(monkeypatch):
    slept = []
    responses = [[], _advisories(45)]
    monkeypatch.setattr(dep, 'get_waterbody_count', lambda *a, **k: 45)
    monkeypatch.setattr(dep, 'get_advisories', lambda **k: responses.pop(0))

    dep.get_complete_advisories(delays=(7, 11), sleep=slept.append)

    assert slept == [7]  # Waited once, before the retry that succeeded


def test_complete_advisories_raises_rather_than_returning_a_partial_list(monkeypatch):
    """A short list is worse than no list: it looks fine and yields a wrong
    count. Callers need the failure so the section is marked unavailable."""
    monkeypatch.setattr(dep, 'get_waterbody_count', lambda *a, **k: 45)
    monkeypatch.setattr(dep, 'get_advisories', lambda **k: _advisories(12))

    with pytest.raises(RuntimeError, match='never returned a complete'):
        dep.get_complete_advisories(delays=(1, 1), sleep=lambda _: None)


def test_complete_advisories_falls_back_to_stability_without_a_count(monkeypatch):
    """If the layer count can't be read we can't verify wholeness, so accept
    the first non-empty reading that repeats rather than trusting one read."""
    responses = [[], _advisories(30), _advisories(45), _advisories(45)]
    monkeypatch.setattr(dep, 'get_waterbody_count',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('arcgis down')))
    monkeypatch.setattr(dep, 'get_advisories', lambda **k: responses.pop(0))

    result = dep.get_complete_advisories(sleep=lambda _: None)

    assert len(result) == 45  # Two 45s in a row
    assert responses == []


def test_complete_advisories_stability_ignores_repeated_empties(monkeypatch):
    """Two empty responses in a row are not a stable reading."""
    responses = [[], [], _advisories(45), _advisories(45)]
    monkeypatch.setattr(dep, 'get_waterbody_count',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))
    monkeypatch.setattr(dep, 'get_advisories', lambda **k: responses.pop(0))

    assert len(dep.get_complete_advisories(sleep=lambda _: None)) == 45

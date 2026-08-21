from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from zoneinfo import ZoneInfo

import dep_advisory as dep

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
        def _get_json(url, **params):
            for key, payload in responses.items():
                if key in url:
                    return payload
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
    assert 'Class SB' in text
    assert '1 of 2 NYC waterbodies' in text


def test_format_post_when_clear():
    advisory = dep.WaterbodyAdvisory.parse(_record('Hudson River (Manhattan)', 0.0))
    assert 'No advisory.' in dep.format_advisory_post(advisory)


def test_post_stays_quiet_when_there_is_no_advisory(fake_api, monkeypatch):
    fake_api({'/advisory': [_record('Hudson River (Manhattan)', 0.0)]})
    posted = []
    monkeypatch.setattr(dep, 'post_message', lambda *a, **k: posted.append(a))
    monkeypatch.setattr(dep, 'post_files', lambda *a, **k: posted.append(a))
    dep.post_waterbody_advisory('test-channel')
    assert posted == []


def test_post_when_clear_flag_posts_text(fake_api, monkeypatch):
    fake_api({'/advisory': [_record('Hudson River (Manhattan)', 0.0)]})
    posted = []
    monkeypatch.setattr(dep, 'post_message', lambda text, **k: posted.append(text))
    dep.post_waterbody_advisory('test-channel', post_when_clear=True)
    assert len(posted) == 1 and 'No advisory.' in posted[0]


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


def test_post_uploads_the_map_when_active(fake_api, capture_uploads):
    fake_api({
        '/advisory': [_record('Hudson River (Manhattan)', 16.0)],
        '/profile': PROFILE,
        '/query': {'features': [_feature(21, [SQUARE])]},
    })
    dep.post_waterbody_advisory('test-channel')

    assert len(capture_uploads['paths']) == 1
    assert capture_uploads['paths'][0].endswith('dep_advisory_map.png')
    assert 'On advisory until' in capture_uploads['comment']


def test_rainfall_is_off_by_default(fake_api, capture_uploads):
    # nws_precip.py already posts precipitation daily, so the gauge chart is
    # opt-in. Requesting rainfall-samples at all would fail the fake_api stub.
    fake_api({
        '/advisory': [_record('Hudson River (Manhattan)', 16.0)],
        '/profile': PROFILE,
        '/query': {'features': [_feature(21, [SQUARE])]},
    })
    dep.post_waterbody_advisory('test-channel')
    assert len(capture_uploads['paths']) == 1
    assert 'gauge:' not in capture_uploads['comment']


def test_rainfall_adds_a_second_image_to_the_same_message(fake_api, capture_uploads):
    fake_api({
        '/advisory': [_record('Hudson River (Manhattan)', 16.0)],
        '/profile': PROFILE,
        '/query': {'features': [_feature(21, [SQUARE])]},
        '/rainfall-samples': [
            {'occurredOn': '2026-08-20T22:00:00', 'precipitation': 1.24},
            {'occurredOn': '2026-08-20T21:00:00', 'precipitation': 0.31},
        ],
        '/sensors': [{'id': 'NR', 'name': 'North River'}],
    })
    dep.post_waterbody_advisory('test-channel', rainfall=True)

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


def test_post_raises_on_unknown_waterbody(fake_api):
    fake_api({'/advisory': [_record('Hudson River (Manhattan)', 0.0)]})
    with pytest.raises(ValueError, match='No waterbody named'):
        dep.post_waterbody_advisory('test-channel', waterbody='Lake Placid')

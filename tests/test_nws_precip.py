import json
import math
import os

import pandas as pd
import pytest

import nykp_conditions.nws_precip as npr


def obs(ts: str, mm_1h=None, mm_3h=None, mm_6h=None, qc='C'):
    """Build one observation's properties, mirroring the api.weather.gov shape."""
    def measurement(value):
        # The API always sends the key, with a null value when there's nothing
        # to report; the QC flag on a null reading is 'Z'.
        return {'unitCode': 'wmoUnit:mm', 'value': value, 'qualityControl': qc if value is not None else 'Z'}

    return {
        'timestamp': ts,
        'precipitationLastHour': measurement(mm_1h),
        'precipitationLast3Hours': measurement(mm_3h),
        'precipitationLast6Hours': measurement(mm_6h),
    }


# The real 2026-07-28 event at KNYC: five SPECIs plus the routine :51 report,
# all restating the same accumulating hour. Naively summed these read 32.3 mm.
STORM_HOUR = [
    obs('2026-07-28T00:06:00+00:00', mm_1h=0.0),
    obs('2026-07-28T00:13:00+00:00', mm_1h=4.1),
    obs('2026-07-28T00:25:00+00:00', mm_1h=6.9),
    obs('2026-07-28T00:34:00+00:00', mm_1h=7.1),
    obs('2026-07-28T00:47:00+00:00', mm_1h=7.1),
    obs('2026-07-28T00:51:00+00:00', mm_1h=7.1),
]


def test_observations_frame_reads_nulls_as_nan():
    df = npr.observations_frame([obs('2026-07-28T01:51:00+00:00')])
    assert len(df) == 1
    assert math.isnan(df['mm_1h'].iloc[0])
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None  # Timestamps are UTC-aware


def test_observations_frame_sorts_by_timestamp():
    # The API returns newest-first; everything downstream assumes ascending.
    df = npr.observations_frame([
        obs('2026-07-28T02:51:00+00:00', mm_1h=2.0),
        obs('2026-07-28T00:51:00+00:00', mm_1h=1.0),
    ])
    assert list(df['mm_1h']) == [1.0, 2.0]


def test_observations_frame_handles_empty_response():
    df = npr.observations_frame([])
    assert df.empty
    assert npr.hourly_precip(df).empty
    assert npr.group_total_mm(df) == 0.0


def test_measurement_rejects_failed_qc():
    failed = obs('2026-07-28T00:51:00+00:00', mm_1h=99.0, qc='X')
    assert math.isnan(npr._measurement(failed, 'precipitationLastHour'))


def test_measurement_keeps_preliminary_qc():
    # Routine observations carry 'C' or 'Z'; filtering those would drop nearly
    # every real reading.
    kept = obs('2026-07-28T00:51:00+00:00', mm_1h=3.0, qc='Z')
    assert npr._measurement(kept, 'precipitationLastHour') == 3.0


def test_hourly_precip_takes_max_not_sum_within_the_hour():
    """The central correctness property: trailing totals must not be summed."""
    hourly = npr.hourly_precip(npr.observations_frame(STORM_HOUR))
    assert hourly.sum() == pytest.approx(7.1)
    assert hourly.max() == pytest.approx(7.1)
    assert hourly.sum() != pytest.approx(32.3)  # What summing would have given


def test_hourly_precip_uses_specis_when_routine_report_is_missing():
    """Regression for 2026-07-21, when the 20:51 report never arrived.

    Reading only the routine :51 observations would have lost this hour's
    1.5 mm entirely.
    """
    observations = [
        obs('2026-07-21T20:03:00+00:00', mm_1h=0.0),
        obs('2026-07-21T20:13:00+00:00', mm_1h=1.5),
        obs('2026-07-21T20:22:00+00:00', mm_1h=1.5),
        obs('2026-07-21T21:51:00+00:00', mm_1h=2.5),
    ]
    hourly = npr.hourly_precip(npr.observations_frame(observations))
    assert hourly.sum() == pytest.approx(4.0)


def test_hourly_precip_treats_reported_nulls_as_zero():
    # A dry hour is reported as null, not 0, and must not become a gap.
    hourly = npr.hourly_precip(npr.observations_frame([obs('2026-07-28T03:51:00+00:00')]))
    assert list(hourly) == [0.0]


def test_hourly_precip_leaves_unobserved_hours_missing():
    """An hour the station never reported is unknown, not dry."""
    observations = [
        obs('2026-07-28T00:51:00+00:00', mm_1h=5.0),
        # No observation at all for 01Z
        obs('2026-07-28T02:51:00+00:00', mm_1h=1.0),
    ]
    hourly = npr.hourly_precip(npr.observations_frame(observations))
    assert len(hourly) == 3
    assert math.isnan(hourly.iloc[1])
    assert hourly.sum() == pytest.approx(6.0)  # NaN skipped, not counted as 0


def test_hourly_precip_converts_to_local_time():
    hourly = npr.hourly_precip(npr.observations_frame(STORM_HOUR))
    assert str(hourly.index.tz) == npr.DEFAULT_TIMEZONE
    # 00Z on 7/28 is 8pm EDT on 7/27
    assert hourly.index[0].hour == 20
    assert hourly.index[0].day == 27


def test_group_total_cross_checks_the_hourly_total():
    observations = STORM_HOUR + [obs('2026-07-28T05:51:00+00:00', mm_6h=7.1)]
    df = npr.observations_frame(observations)
    assert npr.group_total_mm(df) == pytest.approx(npr.hourly_precip(df).sum(), abs=0.1)


@pytest.fixture
def stub_observations(monkeypatch):
    monkeypatch.setattr(npr, 'retrieve_observations', lambda *a, **k: list(STORM_HOUR))


def test_get_observed_precip_reports_totals(stub_observations):
    precip = npr.get_observed_precip()
    assert precip.total_mm == pytest.approx(7.1)
    assert precip.total_inches == pytest.approx(7.1 / 25.4)
    assert precip.station == npr.DEFAULT_STATION
    assert precip.end > precip.start


def test_retrieve_observations_sends_a_user_agent(monkeypatch):
    # api.weather.gov rate-limits generic agents first.
    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps({'features': []}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request):
        captured['ua'] = request.get_header('User-agent')
        return FakeResponse()

    monkeypatch.setattr(npr, 'urlopen', fake_urlopen)
    monkeypatch.setattr(npr.json, 'load', lambda f: {'features': []})
    npr.retrieve_observations('KNYC', pd.Timestamp('2026-07-28', tz='UTC'), pd.Timestamp('2026-07-29', tz='UTC'))
    assert captured['ua'] == npr.USER_AGENT


def test_format_precip_post_reports_the_total(stub_observations):
    text = npr.format_precip_post(npr.get_observed_precip())
    assert '0.28 in' in text
    assert '7.1 mm' in text
    assert 'Wettest hour' in text


def test_format_precip_post_says_so_when_dry(monkeypatch):
    monkeypatch.setattr(npr, 'retrieve_observations',
                        lambda *a, **k: [obs('2026-07-28T03:51:00+00:00')])
    text = npr.format_precip_post(npr.get_observed_precip())
    assert 'No measurable precipitation' in text


def test_format_precip_post_flags_unobserved_hours(monkeypatch):
    monkeypatch.setattr(npr, 'retrieve_observations', lambda *a, **k: [
        obs('2026-07-28T00:51:00+00:00', mm_1h=5.0),
        obs('2026-07-28T02:51:00+00:00', mm_1h=1.0),
    ])
    text = npr.format_precip_post(npr.get_observed_precip())
    assert 'no observation' in text


def test_plot_precip_file_writes_png(tmp_path, stub_observations):
    target = tmp_path / 'precip.png'
    path = npr.plot_precip_file(npr.get_observed_precip(), path=str(target))
    assert path == str(target)
    assert target.stat().st_size > 0


def test_post_observed_precip_uploads_plot_then_cleans_up(monkeypatch, stub_observations):
    uploaded = {}

    def fake_post_file(path, title, channel, comment=None):
        uploaded['path'] = path
        uploaded['existed_during_upload'] = os.path.exists(path)
        uploaded['comment'] = comment
        return {}

    monkeypatch.setattr(npr, 'post_file', fake_post_file)
    npr.post_observed_precip('test-channel')

    assert uploaded['existed_during_upload'] is True
    assert not os.path.exists(uploaded['path'])
    assert '0.28 in' in uploaded['comment']


def test_post_observed_precip_skips_plot_when_dry(monkeypatch):
    # A flat zero line is noise; dry windows post as text only.
    posted = {}
    monkeypatch.setattr(npr, 'retrieve_observations',
                        lambda *a, **k: [obs('2026-07-28T03:51:00+00:00')])
    monkeypatch.setattr(npr, 'post_message', lambda text, **kw: posted.update(text=text))
    monkeypatch.setattr(npr, 'post_file', lambda *a, **k: pytest.fail('should not upload a plot'))

    npr.post_observed_precip('test-channel')
    assert 'No measurable precipitation' in posted['text']

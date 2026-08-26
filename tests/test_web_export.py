import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import nykp_conditions.web_export as we
from nykp_conditions.dep_advisory import WaterbodyAdvisory
from nykp_conditions.noaa_currents import CurrentsPredictions, Station as CurrentsStation
from nykp_conditions.nws_forecast import ForecastText
from nykp_conditions.nws_precip import ObservedPrecip
from nykp_conditions.water_temps import Station as TempStation

NY = ZoneInfo('America/New_York')


"""----------------------------------------------------------------------------
collect(): the site build must survive a dead upstream source
----------------------------------------------------------------------------"""


def test_collect_marks_a_failed_section_unavailable(capsys):
    """A dead source costs its own section and nothing else."""
    sections = we.collect([
        ('currents', lambda: {'days': []}),
        ('forecast', lambda: (_ for _ in ()).throw(RuntimeError('NWS is down'))),
        ('precip', lambda: {'total_inches': 0.1}),
    ])

    assert sections['currents']['status'] == we.OK
    assert sections['precip']['status'] == we.OK  # Later sections still ran
    assert sections['forecast']['status'] == we.UNAVAILABLE
    assert 'NWS is down' in sections['forecast']['error']


def test_collect_emits_a_warning_annotation_for_each_failure(capsys):
    we.collect([('forecast', lambda: (_ for _ in ()).throw(ValueError('nope')))])
    err = capsys.readouterr().err
    assert '::warning::' in err
    assert 'forecast' in err


def test_collect_keeps_the_payload_alongside_the_status():
    sections = we.collect([('water_temp', lambda: {'low_f': 71.2})])
    assert sections['water_temp'] == {'status': we.OK, 'low_f': 71.2}


def test_build_conditions_lists_unavailable_sections(monkeypatch):
    """The page needs to know what to render as unavailable, and the run must
    still succeed -- a nonzero exit here would block the whole site deploy."""
    def boom(*args, **kwargs):
        raise RuntimeError('down')

    for name in ('build_currents', 'build_water_temp', 'build_forecast',
                 'build_precip', 'build_dep_advisory'):
        monkeypatch.setattr(we, name, boom)

    conditions = we.build_conditions()

    assert sorted(conditions['unavailable']) == sorted(conditions['sections'])
    assert all(s['status'] == we.UNAVAILABLE for s in conditions['sections'].values())
    assert conditions['generated_at']  # Still stamped, so the page can say when


def test_build_conditions_reports_no_unavailable_when_everything_works(monkeypatch):
    for name in ('build_currents', 'build_water_temp', 'build_forecast',
                 'build_precip', 'build_dep_advisory'):
        monkeypatch.setattr(we, name, lambda *a, **k: {'ok': True})
    assert we.build_conditions()['unavailable'] == []


"""----------------------------------------------------------------------------
Section builders
----------------------------------------------------------------------------"""


CURRENTS_CSV = pd.DataFrame({
    'Date_Time (LST_LDT)': [
        '2026-08-23 04:50 AM', '2026-08-23 06:07 AM',
        '2026-08-23 12:05 PM', '2026-08-24 01:03 AM',
    ],
    'Speed (knots)': [0.0, 1.74, -1.84, 0.5],
})


def test_build_currents_groups_by_day_and_reports_speed_magnitude(monkeypatch):
    """Ebb speeds are signed in the CSV; a page showing '-2.1 mph' is nonsense
    when the stage already says which way the water is going."""
    monkeypatch.setattr(
        we, 'retrieve_currents_table',
        lambda station=None, **kw: CurrentsPredictions(CURRENTS_CSV.copy(), 'https://noaa.test/x'),
    )
    section = we.build_currents(days=2)

    assert [d['date'] for d in section['days']] == ['2026-08-23', '2026-08-24']
    assert section['days'][0]['date_display'] == 'Sunday, August 23'

    entries = section['days'][0]['entries']
    assert entries[0]['stage'] == 'slack'
    assert entries[0]['speed_mph'] is None  # No speed shown for slack water
    assert entries[1]['stage'] == 'max flood'
    assert entries[2]['stage'] == 'max ebb'
    assert entries[2]['speed_mph'] > 0  # Magnitude, not the signed value


def test_build_currents_honors_the_day_limit(monkeypatch):
    monkeypatch.setattr(
        we, 'retrieve_currents_table',
        lambda station=None, **kw: CurrentsPredictions(CURRENTS_CSV.copy(), 'https://noaa.test/x'),
    )
    assert len(we.build_currents(days=1)['days']) == 1


def test_build_water_temp_reports_the_range_with_local_times(monkeypatch):
    index = pd.DatetimeIndex(
        pd.to_datetime(['2026-08-23T09:00Z', '2026-08-23T18:00Z'], utc=True)
    )
    series = pd.Series([75.0, 76.1], index=index)
    station = TempStation(id='8518750', name='The Battery', lat=40.7, lon=-74.0)
    monkeypatch.setattr(we, 'get_water_temps', lambda *a, **k: (station, series))

    section = we.build_water_temp()

    assert section['low_f'] == 75.0
    assert section['high_f'] == 76.1
    assert section['low_at_display'] == '5am Sun'  # 09:00Z is 5am EDT
    assert '8518750' in section['source_url']
    assert 'image' not in section  # Plot is opt-in


def test_build_forecast_splits_the_periods(monkeypatch):
    """get_forecast_text joins periods with a blank line; the page lays each
    one out separately, so they have to come back apart."""
    monkeypatch.setattr(we, 'get_forecast_text', lambda *a, **k: ForecastText(
        'NWS Forecast for: New York NY', 'Tonight: Mostly clear.\n\nMonday: Sunny.'
    ))
    section = we.build_forecast()
    assert section['periods'] == ['Tonight: Mostly clear.', 'Monday: Sunny.']


def _precip(total_mm=24.3, missing=0):
    start = datetime(2026, 8, 22, 19, tzinfo=NY)
    end = start + timedelta(hours=24)
    hourly = pd.Series(
        [0.0, 12.4, 11.9],
        index=pd.DatetimeIndex([start, start + timedelta(hours=12), end]),
    )
    return ObservedPrecip(
        station='KNYC', station_name='Central Park', hourly=hourly,
        total_mm=total_mm, group_total_mm=total_mm, missing_hours=missing,
        start=start, end=end,
    )


def test_build_precip_reports_totals_and_the_wettest_hour(monkeypatch):
    monkeypatch.setattr(we, 'get_observed_precip', lambda *a, **k: _precip())
    section = we.build_precip()
    assert section['total_inches'] == pytest.approx(0.96, abs=0.01)
    assert section['wettest_hour_inches'] == pytest.approx(0.49, abs=0.01)
    assert section['missing_hours'] == 0


def test_build_precip_omits_the_wettest_hour_when_it_did_not_rain(monkeypatch):
    """A 'wettest hour: 0.00 in' line would be noise on a dry day."""
    dry = _precip(total_mm=0.0)
    dry.hourly[:] = 0.0
    monkeypatch.setattr(we, 'get_observed_precip', lambda *a, **k: dry)
    section = we.build_precip()
    assert section['total_inches'] == 0
    assert 'wettest_hour_display' not in section


"""----------------------------------------------------------------------------
DEP advisory
----------------------------------------------------------------------------"""


def _advisory(name='Hudson River (Manhattan)', duration=0.0, gauge=None):
    return WaterbodyAdvisory(
        waterbody=name, waterbody_id=21, water_class='SB', rain_gauge=gauge,
        occurred_on=datetime(2026, 8, 23, 8, tzinfo=NY),
        duration_hours=duration, message='msg',
    )


def test_dep_advisory_section_goes_unavailable_when_the_dashboard_never_completes(monkeypatch):
    """`get_complete_advisories` raises rather than handing back a partial
    list, and that has to degrade to an unavailable section, not a wrong one."""
    def never_completes(*a, **k):
        raise RuntimeError('DEP dashboard never returned a complete advisory list')

    monkeypatch.setattr(we, 'get_complete_advisories', never_completes)
    sections = we.collect([('dep_advisory', we.build_dep_advisory)])
    assert sections['dep_advisory']['status'] == we.UNAVAILABLE
    assert 'complete advisory list' in sections['dep_advisory']['error']


def test_build_dep_advisory_names_the_known_waterbodies_when_ours_is_missing(monkeypatch):
    monkeypatch.setattr(we, 'get_complete_advisories', lambda *a, **k: [_advisory(name='Gowanus Canal')])
    with pytest.raises(RuntimeError, match='Gowanus Canal'):
        we.build_dep_advisory()


def test_build_dep_advisory_reports_a_clear_waterbody(monkeypatch):
    monkeypatch.setattr(we, 'get_complete_advisories', lambda *a, **k: [_advisory(duration=0.0)])
    section = we.build_dep_advisory()
    assert section['active'] is False
    assert 'until' not in section  # Nothing to count down to
    assert section['total_count'] == 1


def test_build_dep_advisory_reports_an_active_advisory(monkeypatch):
    monkeypatch.setattr(we, 'get_complete_advisories', lambda *a, **k: [_advisory(duration=16.0)])
    section = we.build_dep_advisory()
    assert section['active'] is True
    assert section['hours_remaining'] == 16
    assert section['until_display']
    assert section['active_count'] == 1


def test_build_dep_advisory_includes_rainfall_even_when_clear(monkeypatch):
    """Rainfall is what explains an advisory outlasting the rain, so it's
    reported whether or not one is active."""
    rainfall = pd.Series(
        [0.2, 0.3],
        index=pd.DatetimeIndex([datetime(2026, 8, 23, 6, tzinfo=NY),
                                datetime(2026, 8, 23, 7, tzinfo=NY)], name='hour'),
    )
    monkeypatch.setattr(we, 'get_complete_advisories', lambda *a, **k: [_advisory(gauge='NR')])
    monkeypatch.setattr(we, 'get_rainfall', lambda *a, **k: rainfall)
    monkeypatch.setattr(we, 'get_gauge_names', lambda *a, **k: {'NR': 'North River'})

    section = we.build_dep_advisory()
    assert section['rainfall']['gauge'] == 'North River'
    assert section['rainfall']['last_24h_inches'] == pytest.approx(0.5)


"""----------------------------------------------------------------------------
Output
----------------------------------------------------------------------------"""


def test_img_url_maps_a_written_file_to_its_served_path():
    assert we._img_url('/build/static/img/conditions/forecast.png',
                       '/build/static/img/conditions', '/img/conditions') \
        == '/img/conditions/forecast.png'


def test_img_url_tolerates_a_trailing_slash():
    assert we._img_url('/x/forecast.png', '/x', '/img/conditions/') == '/img/conditions/forecast.png'


def test_img_url_falls_back_to_the_bare_filename():
    assert we._img_url('/x/forecast.png', '/x', None) == 'forecast.png'


def test_write_conditions_writes_parseable_json(tmp_path):
    path = tmp_path / 'nested' / 'conditions.json'
    we.write_conditions({'generated_at': 'now', 'sections': {}}, str(path))
    assert json.loads(path.read_text())['generated_at'] == 'now'


def test_written_json_is_serializable_end_to_end(monkeypatch, tmp_path):
    """pandas Timestamps and numpy floats don't survive json.dump; everything
    the builders emit has to be plain Python by the time it's written."""
    index = pd.DatetimeIndex(pd.to_datetime(['2026-08-23T09:00Z', '2026-08-23T18:00Z'], utc=True))
    station = TempStation(id='8518750', name='The Battery', lat=40.7, lon=-74.0)
    monkeypatch.setattr(we, 'get_water_temps', lambda *a, **k: (station, pd.Series([75.0, 76.1], index=index)))
    monkeypatch.setattr(we, 'retrieve_currents_table',
                        lambda station=None, **kw: CurrentsPredictions(CURRENTS_CSV.copy(), 'u'))
    monkeypatch.setattr(we, 'get_observed_precip', lambda *a, **k: _precip())
    monkeypatch.setattr(we, 'get_forecast_text', lambda *a, **k: ForecastText('t', 'Tonight: x.'))
    monkeypatch.setattr(we, 'get_complete_advisories', lambda *a, **k: [_advisory(duration=16.0)])

    conditions = we.build_conditions()
    assert conditions['unavailable'] == []

    path = we.write_conditions(conditions, str(tmp_path / 'conditions.json'))
    reloaded = json.loads(open(path).read())
    assert reloaded['sections']['currents']['days'][0]['entries'][1]['speed_mph'] > 0

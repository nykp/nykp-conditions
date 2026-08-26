#!/usr/bin/env python3

"""Export the daily conditions as JSON plus plot images, for the NYKP website.

The website builds a conditions page from this: its deploy workflow installs
this package, runs this module to write `data/conditions.json` and the plot
PNGs into `static/`, and Hugo templates render them. Nothing generated is
committed to the site repo -- each build regenerates it.

This shares the fetch and plot layers with the Slack post; only the rendering
differs. `post_conditions.py` turns the same data into Slack markup, this turns
it into JSON that a template can style.

Failure handling differs from the Slack post in one deliberate way. There, a
failed section still makes the run exit nonzero so the failure is visible in
the Actions log. Here a nonzero exit would abort the site deploy, so an
upstream outage would block an unrelated content change from publishing.
Instead each failed section is recorded in the JSON as
`{"status": "unavailable", ...}` for the page to render honestly, reported as a
GitHub Actions warning annotation, and the run still exits 0.
"""

import json
import os
import sys
import traceback
from argparse import ArgumentParser
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pendulum

from .dep_advisory import (
    DASHBOARD_URL,
    HOME_WATERBODY,
    find_advisory,
    get_complete_advisories,
    get_gauge_names,
    get_layer_url,
    get_rainfall,
    get_waterbody_shapes,
    plot_advisory_map,
    plot_rainfall_file,
    recent_total,
    window_hours,
)
from .noaa_currents import (
    NO_READING,
    Station as CurrentsStation,
    current_stage,
    default_nykp_station,
    normalize_currents_table,
    retrieve_currents_table,
)
from .nws_forecast import DEFAULT_LAT_LON, get_forecast_text, save_forecast_plot
from .nws_precip import (
    DEFAULT_STATION as PRECIP_STATION,
    MM_PER_INCH,
    get_observed_precip,
    plot_precip_file,
)
from .utils.format import format_hour
from .utils.scripts import str2bool, try_main
from .utils.units import knots_to_mph
from .water_temps import (
    DEFAULT_HOURS as WATER_TEMP_HOURS,
    OBSERVATIONS_URL_TEMPLATE,
    get_water_temps,
    plot_temps_file,
    temp_range,
)

DEFAULT_TIMEZONE = 'America/New_York'
NWS_FORECAST_URL = (
    'https://forecast.weather.gov/MapClick.php?lat={lat}&lon={lon}'
)

OK = 'ok'
UNAVAILABLE = 'unavailable'


def _iso(when: datetime | pd.Timestamp | None) -> str | None:
    """ISO 8601 with offset, for anything that needs to be re-parsed."""
    return None if when is None else when.isoformat()


def _warn(message: str) -> None:
    """Emit a GitHub Actions warning annotation (plain text off CI)."""
    print(f"::warning::{message}", file=sys.stderr)


def collect(sections: list[tuple[str, Callable[[], dict]]]) -> dict[str, dict]:
    """Run each section builder, isolating failures.

    A section that raises is recorded as unavailable rather than propagating,
    so one dead upstream source costs only its own section -- and never the
    site build.
    """
    out = {}
    for name, build in sections:
        try:
            out[name] = {'status': OK, **build()}
        except Exception as exc:
            traceback.print_exc()
            _warn(f"conditions: {name} unavailable ({type(exc).__name__}: {exc})")
            out[name] = {
                'status': UNAVAILABLE,
                'error': f"{type(exc).__name__}: {exc}",
            }
    return out


"""----------------------------------------------------------------------------
SECTION BUILDERS

Each returns the section's payload, without the 'status' key that `collect`
adds. Raising is fine -- `collect` turns it into an unavailable section.
----------------------------------------------------------------------------"""


def build_currents(station: CurrentsStation | None = None, days: int = 2) -> dict:
    """Tidal current predictions, grouped by day."""
    station = station or default_nykp_station
    predictions = retrieve_currents_table(station=station.id)
    table = normalize_currents_table(predictions.table)

    day_list = []
    for date in sorted(table['date'].unique())[:days]:
        entries = []
        for _, row in table[table['date'] == date].iterrows():
            stage = current_stage(row['knots'])
            speed_mph = None
            if stage not in (NO_READING, 'slack'):
                # Speed is signed to encode direction, which flood/ebb already
                # says, so publish the magnitude.
                speed_mph = round(abs(knots_to_mph(row['knots'])), 1)
            entries.append({
                'time': row['time'],
                'stage': stage,
                'speed_mph': speed_mph,
            })
        day_list.append({
            'date': date,
            'date_display': pendulum.parse(date).format('dddd, MMMM D'),
            'entries': entries,
        })

    return {
        'station': {'name': station.name, 'id': station.id, 'depth': station.depth},
        'source_url': predictions.link,
        'days': day_list,
    }


def build_water_temp(img_dir: str | None = None, img_url: str | None = None,
                     plot: bool = False) -> dict:
    """Water temperature high and low over the last 36 hours."""
    station, temps = get_water_temps()
    rng = temp_range(temps)

    section = {
        'station': {'name': station.name, 'id': station.id},
        'hours': WATER_TEMP_HOURS,
        'low_f': round(rng.low, 1),
        'low_at': _iso(rng.low_at),
        'low_at_display': format_hour(rng.low_at),
        'high_f': round(rng.high, 1),
        'high_at': _iso(rng.high_at),
        'high_at_display': format_hour(rng.high_at),
        'source_url': OBSERVATIONS_URL_TEMPLATE.format(id=station.id),
    }
    if plot and img_dir:
        path = plot_temps_file(temps, station, path=os.path.join(img_dir, 'water-temp.png'))
        section['image'] = _img_url(path, img_dir, img_url)
    return section


def build_forecast(img_dir: str | None = None, img_url: str | None = None) -> dict:
    """NWS text forecast for the next periods, plus the meteogram."""
    text = get_forecast_text()
    section = {
        'title': text.title,
        # `get_forecast_text` joins the periods with a blank line; split them
        # back out so the page can lay each one out on its own.
        'periods': [p for p in text.forecast.split('\n\n') if p.strip()],
        'source_url': NWS_FORECAST_URL.format(
            lat=DEFAULT_LAT_LON.latitude, lon=DEFAULT_LAT_LON.longitude
        ),
    }
    if img_dir:
        path = save_forecast_plot(path=os.path.join(img_dir, 'forecast.png'))
        section['image'] = _img_url(path, img_dir, img_url)
    return section


def build_precip(img_dir: str | None = None, img_url: str | None = None,
                 hours: int = 24) -> dict:
    """Observed precipitation over the last 24 hours."""
    precip = get_observed_precip(station=PRECIP_STATION, hours=hours)

    section = {
        'station': {'id': precip.station, 'name': precip.station_name},
        'hours': hours,
        'total_inches': round(precip.total_inches, 2),
        'total_mm': round(precip.total_mm, 1),
        'start': _iso(precip.start),
        'end': _iso(precip.end),
        'window_display': f"{format_hour(precip.start)} - {format_hour(precip.end)}",
        'missing_hours': precip.missing_hours,
        'source_url': precip.link,
    }
    if precip.total_mm > 0 and not precip.hourly.empty:
        wettest = precip.hourly.idxmax()
        section['wettest_hour_display'] = format_hour(wettest)
        section['wettest_hour_inches'] = round(precip.hourly.max() / MM_PER_INCH, 2)
    if img_dir:
        path = plot_precip_file(precip, path=os.path.join(img_dir, 'precip.png'))
        section['image'] = _img_url(path, img_dir, img_url)
    return section


def build_dep_advisory(img_dir: str | None = None, img_url: str | None = None,
                       waterbody: str = HOME_WATERBODY, rainfall_plot: bool = False) -> dict:
    """DEP advisory status for our stretch of the Hudson, plus the citywide map."""
    # Retries past the dashboard's warm-up: a partial list would put a wrong
    # citywide count on the page rather than failing visibly.
    advisories = get_complete_advisories()
    advisory = find_advisory(advisories, waterbody=waterbody)
    if advisory is None:
        known = ', '.join(sorted(a.waterbody for a in advisories))
        raise RuntimeError(f"No DEP advisory record for {waterbody!r}. Known: {known}")

    section = {
        'waterbody': advisory.waterbody,
        'active': advisory.active,
        'occurred_on': _iso(advisory.occurred_on),
        'occurred_on_display': format_hour(advisory.occurred_on),
        'message': advisory.message,
        'water_class': advisory.water_class,
        'class_description': advisory.class_description,
        'active_count': sum(1 for a in advisories if a.active),
        'total_count': len(advisories),
        # The page says something different on a morning when we're clear but
        # the rest of the city isn't, so it needs both figures, not just ours.
        'elsewhere_active_count': sum(1 for a in advisories
                                      if a.active and a.waterbody != waterbody),
        'source_url': DASHBOARD_URL,
    }
    if advisory.active:
        section['until'] = _iso(advisory.until)
        section['until_display'] = format_hour(advisory.until)
        section['hours_remaining'] = round(advisory.duration_hours)

    # Rainfall is what explains an advisory outlasting the rain, so report it
    # whether or not one is active.
    if advisory.rain_gauge:
        rainfall = get_rainfall(advisory.rain_gauge)
        if not rainfall.empty:
            covered = window_hours(rainfall)
            section['rainfall'] = {
                'gauge': get_gauge_names().get(advisory.rain_gauge, advisory.rain_gauge),
                'last_24h_inches': round(recent_total(rainfall, hours=24), 2),
                'window_hours': covered,
                'window_total_inches': round(float(rainfall.sum()), 2),
            }
            if rainfall_plot and img_dir:
                path = plot_rainfall_file(
                    rainfall, section['rainfall']['gauge'],
                    path=os.path.join(img_dir, 'dep-rainfall.png'),
                )
                section['rainfall']['image'] = _img_url(path, img_dir, img_url)

    if img_dir:
        shapes = get_waterbody_shapes(get_layer_url())
        path = plot_advisory_map(
            advisories, shapes, highlight=waterbody,
            path=os.path.join(img_dir, 'dep-map.png'),
        )
        section['map_image'] = _img_url(path, img_dir, img_url)
    return section


"""----------------------------------------------------------------------------
EXPORT
----------------------------------------------------------------------------"""


def _img_url(path: str, img_dir: str, img_url: str | None) -> str:
    """Map a written file to the URL the site will serve it at."""
    name = os.path.basename(path)
    if img_url is None:
        return name
    return f"{img_url.rstrip('/')}/{name}"


def build_conditions(
        img_dir: str | None = None,
        img_url: str | None = None,
        days: int = 2,
        water_temp_plot: bool = False,
        dep_rainfall_plot: bool = False,
        tz: str = DEFAULT_TIMEZONE,
) -> dict:
    """Collect every section into the document the website renders."""
    now = datetime.now(ZoneInfo(tz))
    sections = collect([
        ('currents', lambda: build_currents(days=days)),
        ('water_temp', lambda: build_water_temp(img_dir, img_url, plot=water_temp_plot)),
        ('forecast', lambda: build_forecast(img_dir, img_url)),
        ('precip', lambda: build_precip(img_dir, img_url)),
        ('dep_advisory', lambda: build_dep_advisory(
            img_dir, img_url, rainfall_plot=dep_rainfall_plot)),
    ])
    unavailable = sorted(k for k, v in sections.items() if v['status'] != OK)
    return {
        'generated_at': _iso(now),
        'generated_at_display': now.strftime('%A, %B %-d, %Y at %-I:%M%p').replace('AM', 'am').replace('PM', 'pm'),
        'timezone': tz,
        'unavailable': unavailable,
        'sections': sections,
    }


def write_conditions(conditions: dict, path: str) -> str:
    """Write the document to `path`, creating parent directories."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w') as f:
        json.dump(conditions, f, indent=2, sort_keys=False)
        f.write('\n')
    return path


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=str, default='conditions.json',
                        help='Path to write the JSON document to')
    parser.add_argument('--img-dir', type=str, default=None,
                        help='Directory to write plot PNGs into (omit to skip plots)')
    parser.add_argument('--img-url', type=str, default=None,
                        help="URL prefix the images are served at, e.g. '/img/conditions'")
    parser.add_argument('--days', type=int, default=2,
                        help='Days of current predictions to include (default: 2)')
    parser.add_argument('--water-temp-plot', type=str2bool, default=False,
                        help='Include the water temperature plot (default: False)')
    parser.add_argument('--dep-rainfall-plot', type=str2bool, default=False,
                        help='Include the DEP gauge rainfall chart (default: False)')
    return parser


def main(args):
    if args.img_dir:
        os.makedirs(args.img_dir, exist_ok=True)
    conditions = build_conditions(
        img_dir=args.img_dir,
        img_url=args.img_url,
        days=args.days,
        water_temp_plot=args.water_temp_plot,
        dep_rainfall_plot=args.dep_rainfall_plot,
    )
    path = write_conditions(conditions, args.out)

    unavailable = conditions['unavailable']
    total = len(conditions['sections'])
    print(f"Wrote {path}: {total - len(unavailable)} of {total} sections available")
    if unavailable:
        # Visible in the log and as annotations, but not fatal: the site still
        # deploys with those sections marked unavailable.
        print(f"Unavailable: {', '.join(unavailable)}")


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

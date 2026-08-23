"""Observed precipitation from NWS station observations.

The station reports three precipitation fields per observation
(`precipitationLastHour`, `precipitationLast3Hours`, `precipitationLast6Hours`),
and turning them into an hourly series takes some care:

* `null` means "no precipitation", not "no data". KNYC leaves all three fields
  null on a dry day, so nulls are read as zero rather than dropped.
* The values are *trailing totals*, not per-observation amounts, and during
  active weather the station issues extra observations (SPECIs) between the
  routine hourly ones at :51. A single hour of rain on 2026-07-28 appeared as
  0.0, 4.1, 6.9, 7.1, 7.1, 7.1 mm across six observations -- all reporting the
  same accumulating hour. Summing them gives 32.3 mm instead of 7.1 mm.

So the hourly series takes the **maximum** `precipitationLastHour` within each
clock hour. Taking only the routine :51 observation instead would undercount
whenever that observation is missing: on 2026-07-21 the 20:51 report never
arrived, and only the SPECIs recorded that hour's 1.5 mm.

The 6-hour groups are then summed as an independent cross-check. Over the week
ending 2026-07-28 the two agreed to within 0.1 mm (12.6 vs 12.7), while the
naive sum of every hourly value read 39.6 mm.
"""

import json
import os
import tempfile
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.dates import DateFormatter

from .utils.format import format_hour
from .utils.scripts import try_main
from .utils.slack import NykpSlackChannels, post_file, post_message

OBSERVATIONS_URL_BASE = 'https://api.weather.gov/stations/{station}/observations'
STATION_PAGE_TEMPLATE = 'https://www.weather.gov/wrh/timeseries?site={station}'
# api.weather.gov asks callers to identify themselves; unattended requests from
# a generic agent are the ones they rate-limit first.
USER_AGENT = 'nykp-conditions (https://github.com/nykp/noaa-currents)'

DEFAULT_STATION = 'KNYC'  # Central Park
DEFAULT_STATION_NAME = 'Central Park'
DEFAULT_TIMEZONE = 'America/New_York'
DATETIME_FMT = '%Y-%m-%dT%H:%M:%SZ'
MM_PER_INCH = 25.4

# NWS quality-control flags. 'X' marks a reading that failed validation; the
# rest ('V' validated, 'C' coarse pass, 'S' subjective, 'Z' preliminary) are
# kept, since routine observations sit at 'Z' or 'C' and filtering them would
# discard nearly everything.
REJECTED_QC_FLAGS = frozenset({'X'})

PRECIP_FIELDS = {
    'mm_1h': 'precipitationLastHour',
    'mm_3h': 'precipitationLast3Hours',
    'mm_6h': 'precipitationLast6Hours',
}


@dataclass
class ObservedPrecip:
    """Hourly precipitation over a window, with the totals derived from it."""

    station: str
    station_name: str
    hourly: pd.Series  # mm per clock hour, indexed in local time
    total_mm: float
    group_total_mm: float  # Independent total from the 6-hour groups
    missing_hours: int  # Hours in the window with no observation at all
    start: datetime
    end: datetime

    @property
    def total_inches(self) -> float:
        return self.total_mm / MM_PER_INCH

    @property
    def link(self) -> str:
        return STATION_PAGE_TEMPLATE.format(station=self.station)


def get_observations_url(station: str, start_dt: datetime, end_dt: datetime) -> str:
    """URL for a station's observations over a time window (UTC)."""
    start = start_dt.astimezone(timezone.utc).strftime(DATETIME_FMT)
    end = end_dt.astimezone(timezone.utc).strftime(DATETIME_FMT)
    return f'{OBSERVATIONS_URL_BASE.format(station=station)}?start={start}&end={end}'


def retrieve_observations(
        station: str, start_dt: datetime, end_dt: datetime, path: str | None = None
) -> list[dict]:
    """Fetch raw observation properties for a station over a window.

    Pass `path` to also keep the raw JSON response at that location.
    """
    url = get_observations_url(station, start_dt, end_dt)
    request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/geo+json'})
    with urlopen(request) as resp:
        payload = json.load(resp)
    if path is not None:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w') as f:
            json.dump(payload, f)
    return [feature['properties'] for feature in payload.get('features', [])]


def _measurement(observation: dict, field: str) -> float:
    """Pull one precipitation measurement out of an observation, in mm.

    Missing, null, and QC-rejected readings all come back as NaN so they can be
    told apart from a real zero downstream.
    """
    measurement = observation.get(field) or {}
    if measurement.get('qualityControl') in REJECTED_QC_FLAGS:
        return float('nan')
    value = measurement.get('value')
    return float('nan') if value is None else float(value)


def observations_frame(observations: list[dict]) -> pd.DataFrame:
    """Build a UTC-indexed frame of the three precipitation fields, in mm."""
    rows = [
        {'ts': obs['timestamp'], **{col: _measurement(obs, field) for col, field in PRECIP_FIELDS.items()}}
        for obs in observations
        if obs.get('timestamp')
    ]
    df = pd.DataFrame(rows, columns=['ts', *PRECIP_FIELDS])
    df['ts'] = pd.to_datetime(df['ts'], utc=True, format='ISO8601')
    return df.set_index('ts').sort_index()


def hourly_precip(df: pd.DataFrame, tz: str = DEFAULT_TIMEZONE) -> pd.Series:
    """Collapse trailing-hour readings into one value per clock hour, in mm.

    Takes the maximum reading in each hour rather than the sum: see the module
    docstring. Hours that had observations but no reported precipitation come
    back as 0.0; hours with no observation at all stay NaN, so a gap in the
    station's reporting isn't silently counted as a dry hour.
    """
    if df.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz=tz, name='ts'))
    resampled = df['mm_1h'].resample('1h')
    hourly = resampled.max()
    observed = resampled.size() > 0
    hourly[observed & hourly.isna()] = 0.0
    return hourly.tz_convert(tz)


def group_total_mm(df: pd.DataFrame) -> float:
    """Total from the 6-hour groups, which partition the window without overlap.

    Used only as a sanity check on the hourly total; the groups are reported at
    synoptic hours, so they cover the window more coarsely.
    """
    return float(df['mm_6h'].sum()) if not df.empty else 0.0


def get_observed_precip(
        station: str = DEFAULT_STATION,
        as_of: datetime | None = None,
        hours: int = 24,
        tz: str = DEFAULT_TIMEZONE,
        station_name: str = DEFAULT_STATION_NAME,
) -> ObservedPrecip:
    """Observed precipitation at a station over the last `hours`."""
    zone = ZoneInfo(tz)
    if as_of is None:
        as_of = datetime.now(tz=zone)
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=zone)
    start_dt = as_of - timedelta(hours=hours)

    observations = retrieve_observations(station, start_dt, as_of)
    df = observations_frame(observations)
    hourly = hourly_precip(df, tz=tz)
    return ObservedPrecip(
        station=station,
        station_name=station_name,
        hourly=hourly,
        total_mm=float(hourly.sum()),
        group_total_mm=group_total_mm(df),
        missing_hours=int(hourly.isna().sum()),
        start=start_dt.astimezone(zone),
        end=as_of.astimezone(zone),
    )


def plot_precip_file(precip: ObservedPrecip, path: str | None = None) -> str:
    """Plot the hourly series to a PNG, returning the path written.

    Hourly rates only: the window total goes in the message text instead, where
    it reads more precisely than it would off a second axis.
    """
    if path is None:
        fd, path = tempfile.mkstemp(prefix=f'precip_{precip.station}_', suffix='.png')
        os.close(fd)

    hourly_in = precip.hourly / MM_PER_INCH
    title = (f'Precipitation at {precip.station_name}, '
             f'{format_hour(precip.start)} - {format_hour(precip.end)}')

    sns.set_theme()
    fig, ax = plt.subplots(figsize=(10, 4))
    try:
        # Bars are an hour wide in date units, aligned to the hour they cover.
        ax.bar(hourly_in.index, hourly_in.values, width=1 / 24, align='edge')
        ax.set_ylabel('Hourly (in)')
        ax.set_ylim(bottom=0)
        # matplotlib renders tz-aware timestamps in UTC unless told otherwise,
        # which would shift every label by the station's offset.
        ax.xaxis.set_major_formatter(DateFormatter('%-I%p', tz=hourly_in.index.tz))
        ax.set_title(title)

        fig.autofmt_xdate(rotation=45)
        fig.savefig(path, bbox_inches='tight', pad_inches=0.25)
    finally:
        plt.close(fig)

    return path


def format_precip_post(precip: ObservedPrecip) -> str:
    """Build the Slack message body for the observed precipitation."""
    window = f"{format_hour(precip.start)} - {format_hour(precip.end)}"
    header = (f"<{precip.link}|Recent precipitation measured at {precip.station_name} "
              f"({precip.station})>, {window}")
    if precip.total_mm == 0:
        return f"{header}\n*No measurable precipitation.*"

    lines = [header, f"*{precip.total_inches:.2f} in* ({precip.total_mm:.1f} mm) total"]
    wettest = precip.hourly.idxmax()
    lines.append(f"Wettest hour: {format_hour(wettest)} "
                 f"({precip.hourly.max() / MM_PER_INCH:.2f} in)")
    if precip.missing_hours:
        lines.append(f"_{precip.missing_hours} hour(s) had no observation and are not counted._")
    return '\n'.join(lines)


def post_observed_precip(
        channel: str, station: str = DEFAULT_STATION, hours: int = 24
) -> None:
    """Post observed precipitation for the last `hours` to Slack.

    A plot of a flat zero line says nothing, so dry windows post as text only.
    """
    precip = get_observed_precip(station=station, hours=hours)
    print(f"Observed precip: {precip.total_mm:.1f} mm hourly total, "
          f"{precip.group_total_mm:.1f} mm from 6-hour groups (cross-check), "
          f"{precip.missing_hours} hour(s) unobserved")

    post_text = format_precip_post(precip)
    if precip.total_mm == 0:
        post_message(post_text, channel=channel, unfurl_links=False)
        return

    # Keep the plot in a temp dir that's cleaned up only after the upload
    # returns (success or failure), so the file can't vanish mid-upload.
    with tempfile.TemporaryDirectory() as tmp_dir:
        plot_img_path = os.path.join(tmp_dir, f'precip_{precip.station}.png')
        plot_precip_file(precip, path=plot_img_path)
        post_file(plot_img_path, title='Observed precipitation', channel=channel, comment=post_text)


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--station', type=str, default=DEFAULT_STATION)
    parser.add_argument('--channel', type=str, default=NykpSlackChannels.test_python_api)
    parser.add_argument('--hours', type=int, default=24)
    return parser


def main(args):
    post_observed_precip(args.channel, station=args.station, hours=args.hours)


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

"""NOAA tidal current predictions (slack/max flood/max ebb) for the Hudson.

Predictions come from the tidesandcurrents CSV download endpoint, which is
formatted into a compact per-day summary for the daily Slack post.
"""

import os
import tempfile
from argparse import ArgumentParser
from collections.abc import Sequence
from dataclasses import dataclass, fields
from urllib.request import urlretrieve

import pandas as pd
import pendulum

from utils.data import rename_cols
from utils.scripts import try_main
from utils.slack import NykpSlackChannels, post_message, post_file
from utils.units import knots_to_mph

CURRENTS_CSV_URL_BASE = 'https://api.tidesandcurrents.noaa.gov/dpapi/prod/webapi/currentPredictionsDownload'
CURRENTS_CSV_DATE_FMT = 'YYYY-MM-DD'
ONE_WEEK_STRS = ('w', '1w')
TWO_DAY_STRS = ('48h', '2d')

# Keys are matched as substrings of the CSV header, whose units vary by
# endpoint ('Speed (knots)' vs 'Speed (cm/s)').
COLUMN_RENAMES = {'Date_Time': 'datetime', 'Speed': 'knots'}
SLACK_THRESHOLD_KNOTS = 0.1
NO_READING = 'no reading'

@dataclass
class Station:
    name: str
    id: str
    depth: str | None = None

    def __repr__(self):
        s = f"{self.name} ({self.id})"
        if self.depth is not None:
            s += f", {self.depth}"
        return s


class StationLibrary:
    Hudson_River_Pier_92 = Station('Hudson River, Pier 92', 'NYH1928_12', '6 feet')
    Hudson_River_Entrance = Station('Hudson River Entrance', 'NYH1927_13', '7 feet')


default_nykp_station = StationLibrary.Hudson_River_Pier_92


@dataclass
class CurrentsPredictions:
    table: pd.DataFrame
    link: str
    plot_img_path: str | None = None

    def __repr__(self):
        lines = [
            type(self).__name__,
            '\n'.join([repr(getattr(self, field.name)) for field in fields(self)]),
            '',
        ]
        return '\n'.join(lines)


def _starts_with_one_of(s: str, candidates: str | Sequence[str], ignore_case=True) -> bool:
    if isinstance(candidates, str):
        candidates = [candidates]
    if ignore_case:
        s = s.lower()
        candidates = [c.lower() for c in candidates]
    return any([s.startswith(c) for c in candidates])


def parse_time_period(r: int | str | None) -> int:
    """Normalize a time-period argument to the API's range code.

    Accepts the codes themselves (1 = 48 hours, 2 = weekly) or friendly
    strings ('48h', '2d', 'w', '1w'). Defaults to 48 hours.
    """
    if r is None:
        return 1  # Default
    if isinstance(r, str):
        try:
            r = int(r)
        except ValueError:
            pass
    if isinstance(r, int):
        if r not in (1, 2):
            raise ValueError(f"Invalid integer value for range: {r}. Accepted values: {{1 = 48 hrs, 2 = Weekly}}")
        return r
    if _starts_with_one_of(r, ONE_WEEK_STRS):
        return 2
    if _starts_with_one_of(r, TWO_DAY_STRS):
        return 1
    raise ValueError(
        f"Invalid value for range: {r!r}. Accepted values: {ONE_WEEK_STRS + TWO_DAY_STRS}, or 1 (48 hrs) / 2 (weekly)"
    )


def currents_csv_url(station_id: str, date: pendulum.DateTime, time_period: int) -> str:
    """Build the CSV download URL for a station's current predictions."""
    csv_url_params = {
        'id': station_id,
        'start_date': date.format(CURRENTS_CSV_DATE_FMT),
        'range': time_period,  # range {1 = 48 hrs, 2 = Weekly}
        'date_timeUnits': 'am%2fpm',
        'interval': 'MAX_SLACK',
        'threshold': 'leEq',
        'thresholdValue': '',
        'time_zone': 'LST_LDT',
        'units': 1,
        'format': 'csv',
    }
    return CURRENTS_CSV_URL_BASE + '?' + '&'.join([f"{k}={v}" for k, v in csv_url_params.items()]) + '&'


def retrieve_currents_table(
        station: str | Station,
        date: str | pendulum.DateTime | None = None,
        time_period=None,
) -> CurrentsPredictions:
    """Download current predictions for a station and return the parsed table.

    The CSV is fetched into a temp directory that is removed on the way out,
    success or failure, so nothing is left behind in the working directory.
    """
    if date is None:
        date = pendulum.today()
    elif isinstance(date, str):
        date = pendulum.parse(date)

    if isinstance(station, str):
        if hasattr(StationLibrary, station):
            station_id = getattr(StationLibrary, station).id
        else:
            station_id = station
    else:
        station_id = station.id

    # Get csv of data
    time_period = parse_time_period(time_period)
    csv_url = currents_csv_url(station_id, date, time_period)
    station_link = f"https://tidesandcurrents.noaa.gov/noaacurrents/predictions.html?id={station_id}"
    with tempfile.TemporaryDirectory() as tmp_dir:
        csv_path = os.path.join(tmp_dir, f"current_predictions_{station_id}_{date}.csv")
        (_, headers) = urlretrieve(csv_url, csv_path)
        try:
            df = pd.read_csv(csv_path)
        except Exception:
            # The endpoint returns an error page with a 200, so surface what
            # came back rather than only the parse error.
            print(f"Failed to parse currents CSV from {csv_url}\nResponse headers:\n{headers}")
            raise
    return CurrentsPredictions(df, station_link)


def current_stage(knots) -> str:
    """Label a signed speed as max flood, max ebb, or slack.

    A blank cell in NOAA's CSV parses to NaN, which compares False against both
    thresholds; untested, that reported a missing reading as slack water.
    """
    if pd.isna(knots):
        return NO_READING
    if knots > SLACK_THRESHOLD_KNOTS:
        return 'max flood'
    if knots < -SLACK_THRESHOLD_KNOTS:
        return 'max ebb'
    return 'slack'


def normalize_currents_table(df: pd.DataFrame) -> pd.DataFrame:
    """Rename the CSV's columns and split its timestamps into date and time."""
    df = rename_cols(df, COLUMN_RENAMES)
    df['date'] = df['datetime'].map(lambda dts: dts.split(' ')[0])
    df['time'] = df['datetime'].map(lambda dts: ' '.join(dts.split(' ')[1:]))
    return df


def format_currents_table(df: pd.DataFrame, date=None) -> str:
    """Render one day's predictions as lines of 'time  stage (speed)'."""
    if date is None:
        date = pendulum.today()
    if not isinstance(date, str):
        date = date.format('YYYY-MM-DD')
    df = normalize_currents_table(df)

    lines = []
    for _, row in df[df['date'] == date].iterrows():
        stage = current_stage(row['knots'])
        line = f"{row['time']}  {stage}"
        if stage not in (NO_READING, 'slack'):
            # Speed is signed to encode direction, which 'flood'/'ebb' already
            # says, so show the magnitude rather than a confusing "-1.4 mph".
            line += f" ({abs(knots_to_mph(row['knots'])):.1f} mph)"
        lines.append(line)
    return '\n'.join(lines)


def format_currents_post(predictions: CurrentsPredictions, station: Station, days: int = 1) -> str:
    """Build the Slack message body: a header link plus one block per day."""
    dates = sorted(normalize_currents_table(predictions.table)['date'].unique())
    header = f"<{predictions.link}|NOAA current predictions at {station.name} (depth: {station.depth})>"
    blocks = []
    for d in dates[:days]:
        date_str = pendulum.parse(d).format('dddd, MMMM D, YYYY')
        blocks.append(f"*{date_str}*\n{format_currents_table(predictions.table, date=d)}")
    return '\n'.join([header] + blocks)


def post_currents(channel: str, station: Station | None = None, date=None, time_period=None, days=1) -> None:
    """Post current predictions for the next `days` days to Slack."""
    if station is None:
        station = default_nykp_station
    predictions = retrieve_currents_table(station=station.id, date=date, time_period=time_period)
    post_message(format_currents_post(predictions, station, days=days), channel=channel, unfurl_links=False)
    if predictions.plot_img_path:
        post_file(predictions.plot_img_path, title=f"Currents at {station}", channel=channel)


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--date', type=str, default=None)
    parser.add_argument('--station', type=str, default=None)
    parser.add_argument('--range', type=str, default=None)
    parser.add_argument('--channel', type=str, default=None)
    parser.add_argument('--days', type=int, default=1)
    return parser


def main(args):
    if args.channel is not None:
        channel = args.channel
    else:
        channel = NykpSlackChannels.test_python_api

    if args.station is not None:
        station = Station(id=args.station, name=f"Station {args.station}")
    else:
        station = None

    post_currents(channel, station=station, date=args.date, time_period=args.range, days=args.days)


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

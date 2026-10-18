"""NOAA water temperature observations and the summary posted from them.

The post reports the range -- the low and high of the window and when each was
measured -- rather than the full series. The hour-to-hour detail in a plot is
noise for the question the post answers ("how cold is the water right now?"),
so the timeseries plot is available behind `plot=True` but off by default.
"""

import json
import os
import tempfile
from argparse import ArgumentParser
from dataclasses import dataclass

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.dates import DateFormatter

from .utils.format import format_hour
from .utils.http import urlopen, urlretrieve
from .utils.plot import DateTimeFormats
from .utils.scripts import str2bool, try_main
from .utils.slack import NykpSlackChannels, post_file, post_message

WATER_TEMPS_URL_BASE = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
OBSERVATIONS_URL_TEMPLATE = 'https://tidesandcurrents.noaa.gov/stationhome.html?id={id}#obs'
THE_BATTERY_STATION_ID = '8518750'
DEFAULT_TIMEZONE = 'America/New_York'
DEFAULT_HOURS = 36


@dataclass
class Station:
    id: str
    name: str
    lat: float
    lon: float


@dataclass
class TempRange:
    """The low and high of a temperature series, with when each was measured."""

    low: float
    low_at: pd.Timestamp
    high: float
    high_at: pd.Timestamp


def get_water_temps(
        station_id: str | None = None, hours: int = DEFAULT_HOURS, units='english', path=None
) -> tuple[Station, pd.Series]:
    """Fetch hourly water temperatures for a station over the last `hours`.

    Returns the station metadata and a time-indexed (UTC) temperature series.
    Pass `path` to also keep the raw JSON response at that location.
    """
    if station_id is None:
        station_id = THE_BATTERY_STATION_ID
    params = {
        'product': 'water_temperature',
        'station': station_id,  # The Battery
        'range': hours,  # hours
        'interval': 'h',
        'format': 'json',
        'units': units,
        'time_zone': 'gmt',  # Setting UTC timezone below
    }
    param_str = '&'.join(f'{k}={v}' for k, v in params.items())
    url = f'{WATER_TEMPS_URL_BASE}?{param_str}'
    if path is None:
        # We only parse the response, so read it straight into memory.
        with urlopen(url) as resp:
            data_dct = json.load(resp)
    else:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        urlretrieve(url, path)
        with open(path) as f:
            data_dct = json.load(f)

    station_info = Station(**data_dct['metadata'])
    water_temps = pd.DataFrame(data_dct['data']).set_index('t')['v']
    water_temps.index = pd.DatetimeIndex(pd.to_datetime(water_temps.index, utc=True))
    # NOAA sends the values as strings, and comparing those is lexicographic:
    # '9.5' would sort above '70.1' and report a bogus high in cold weather.
    water_temps = pd.to_numeric(water_temps, errors='coerce').dropna()
    return station_info, water_temps


def temp_range(water_temps: pd.Series, tz: str = DEFAULT_TIMEZONE) -> TempRange:
    """Low and high of the series, timestamped in local time."""
    if water_temps.empty:
        raise ValueError('No water temperature observations to summarize')
    local = water_temps.tz_convert(tz)
    return TempRange(
        low=float(local.min()),
        low_at=local.idxmin(),
        high=float(local.max()),
        high_at=local.idxmax(),
    )


def format_temps_post(station: Station, temps: TempRange, hours: int = DEFAULT_HOURS) -> str:
    """Build the Slack message body for the water temperature range."""
    observations_url = OBSERVATIONS_URL_TEMPLATE.format(id=station.id)
    return '\n'.join([
        f"<{observations_url}|NOAA water temperature observations at {station.name} "
        f"for the last {hours} hours>",
        f"*Low {temps.low:.1f}°F* at {format_hour(temps.low_at)}",
        f"*High {temps.high:.1f}°F* at {format_hour(temps.high_at)}",
    ])


def plot_temps_file(
        water_temps: pd.Series, station: Station, path=None, tz: str = DEFAULT_TIMEZONE
) -> str:
    """Plot the temperature series to a PNG, returning the path written.

    Plotted in local time to agree with the message text, which reports its
    times via `temp_range`. The series arrives UTC-indexed, and matplotlib
    renders tz-aware timestamps in UTC unless told otherwise, so an unconverted
    plot contradicted the text it was posted with by four hours.
    """
    local = water_temps.tz_convert(tz)
    title_dt_fmt = '%H:%M:%S %m/%d/%Y'
    title = (f'{local.index[0].strftime(title_dt_fmt)} - '
             f'{local.index[-1].strftime(title_dt_fmt)} at {station.name}')

    if path is None:
        fd, path = tempfile.mkstemp(prefix=f'water_temps_{station.id}_', suffix='.png')
        os.close(fd)

    sns.set_theme()
    plt.figure(figsize=(10, 4))
    try:
        plt.plot(local.rolling(10, center=True, win_type='boxcar').mean())
        plt.gca().xaxis.set_major_formatter(
            DateFormatter(DateTimeFormats.h_m_s, tz=local.index.tz)
        )
        plt.xticks(rotation=45)
        plt.ylabel('Water Temp. (F)')
        plt.title(title)
        plt.savefig(path, bbox_inches='tight', pad_inches=0.25)
    finally:
        plt.close()

    return path


def post_water_temps(
        channel: str, station: str | None = None, hours: int = DEFAULT_HOURS, plot: bool = False
) -> None:
    """Post the water temperature range for the last `hours` to Slack.

    Pass `plot=True` to attach the timeseries plot to the same message.
    """
    station, water_temps = get_water_temps(station_id=station, hours=hours)
    post_txt = format_temps_post(station, temp_range(water_temps), hours=hours)
    if not plot:
        post_message(post_txt, channel=channel, unfurl_links=False)
        return

    # Keep the plot in a temp dir that's cleaned up only after the upload
    # returns (success or failure), so the file can't vanish mid-upload.
    with tempfile.TemporaryDirectory() as tmp_dir:
        plot_img_path = os.path.join(tmp_dir, f'water_temps_{station.id}.png')
        plot_temps_file(water_temps, station, path=plot_img_path)
        post_file(plot_img_path, title="Water temperature", channel=channel, comment=post_txt)


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--station', type=str, default=THE_BATTERY_STATION_ID)
    parser.add_argument('--channel', type=str, default=NykpSlackChannels.test_python_api)
    parser.add_argument('--hours', type=int, default=DEFAULT_HOURS)
    parser.add_argument('--plot', type=str2bool, default=False,
                        help='Attach the timeseries plot (default: False)')
    return parser


def main(args):
    post_water_temps(args.channel, station=args.station, hours=args.hours, plot=args.plot)


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

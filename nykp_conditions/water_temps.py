"""NOAA water temperature observations and the plot posted with them."""

import json
import os
import tempfile
from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.request import urlopen, urlretrieve

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.dates import DateFormatter

from utils.plot import DateTimeFormats
from utils.scripts import try_main
from utils.slack import NykpSlackChannels, post_file

WATER_TEMPS_URL_BASE = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
OBSERVATIONS_URL_TEMPLATE = 'https://tidesandcurrents.noaa.gov/stationhome.html?id={id}#obs'
THE_BATTERY_STATION_ID = '8518750'


@dataclass
class Station:
    id: str
    name: str
    lat: float
    lon: float


def get_water_temps(
        station_id: Optional[str] = None, hours: int = 36, units='english', path=None
) -> Tuple[Station, pd.Series]:
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
    return station_info, water_temps


def plot_temps_file(water_temps: pd.Series, station: Station, path=None) -> str:
    """Plot the temperature series to a PNG, returning the path written."""
    start_dt = water_temps.index[0]
    end_dt = water_temps.index[-1]
    title_dt_fmt = '%H:%M:%S %m/%d/%Y'
    title = f'{start_dt.strftime(title_dt_fmt)} - {end_dt.strftime(title_dt_fmt)} at {station.name}'
    ylabel = 'Water Temp. (F)'
    xtick_dt_fmt = DateTimeFormats.h_m_s

    if path is None:
        fd, path = tempfile.mkstemp(prefix=f'water_temps_{station.id}_', suffix='.png')
        os.close(fd)

    sns.set_theme()
    plt.figure(figsize=(10, 4))
    try:
        plt.plot(water_temps.rolling(10, center=True, win_type='boxcar').mean())
        plt.gca().xaxis.set_major_formatter(DateFormatter(xtick_dt_fmt))
        plt.xticks(rotation=45)
        plt.ylabel(ylabel)
        plt.title(title)
        plt.savefig(path, bbox_inches='tight', pad_inches=0.25)
    finally:
        plt.close()

    return path


def post_water_temps(channel: str, station: Optional[str] = None) -> None:
    """Post the water temperature plot for the last 36 hours to Slack."""
    station, water_temps = get_water_temps(station_id=station)
    observations_url = OBSERVATIONS_URL_TEMPLATE.format(id=station.id)
    post_txt = f"<{observations_url}|NOAA water temperature observations at {station.name} for the last 36 hours>\n"
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
    return parser


def main(args):
    post_water_temps(args.channel, station=args.station)


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

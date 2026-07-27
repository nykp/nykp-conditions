import json
import os
from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Optional
from urllib.request import urlretrieve

import matplotlib.pyplot as plt
import pandas as pd
import pendulum
import seaborn as sns
from matplotlib.dates import DateFormatter
from matplotlib.transforms import Bbox

from utils.plot import DateTimeFormats, format_time_axis
from utils.scripts import try_main
from utils.slack import NykpSlackChannels, post_message, post_file

WATER_TEMPS_URL_BASE = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
OBSERVATIONS_URL_TEMPLATE = 'https://tidesandcurrents.noaa.gov/stationhome.html?id={id}#obs'
THE_BATTERY_STATION_ID = '8518750'

CACHE_DIR = './cache'
_cleanup_paths = []


def _init_cache(path=CACHE_DIR):
    path_dirs = path.split('/')
    node = path_dirs[0]
    if not os.path.exists(node):
        _cleanup_paths.append(node)
    for d in path_dirs[1:]:
        node += '/' + d
        if not os.path.exists(node):
            _cleanup_paths.append(node)


@dataclass
class Station:
    id: str
    name: str
    lat: float
    lon: float


def get_water_temps(
        station_id: Optional[str] = None, hours: int = 36, units='english', path=None
) -> (Station, pd.Series):
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
        now = pendulum.now().format('YYYYMMDD_HHmmss')
        filename = f'water_temps_{station_id}_{now}_{hours}h.json'
        path = os.path.join(CACHE_DIR, filename)
        _cleanup_paths.append(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    urlretrieve(url, path)
    with open(path) as f:
        data_dct = json.load(f)

    station_info = Station(**data_dct['metadata'])
    water_temps = pd.DataFrame(data_dct['data']).set_index('t')['v']
    water_temps.index = pd.DatetimeIndex(pd.to_datetime(water_temps.index, utc=True))
    return station_info, water_temps


def plot_temps_file(water_temps: pd.Series, station: Station, path=None, keep=False):
    start_dt = water_temps.index[0]
    end_dt = water_temps.index[-1]
    title_dt_fmt = '%H:%M:%S %m/%d/%Y'
    title = f'{start_dt.strftime(title_dt_fmt)} - {end_dt.strftime(title_dt_fmt)} at {station.name}'
    ylabel = 'Water Temp. (F)'
    xtick_dt_fmt = DateTimeFormats.h_m_s

    if path is None:
        now = pendulum.now().format('YYYYMMDD_HHmmss')
        filename = f'water_temps_{station.id}_{now}.png'
        path = os.path.join(CACHE_DIR, filename)
        if not keep:
            _cleanup_paths.append(path)

    sns.set_theme()
    plt.figure(figsize=(10, 4))
    plt.plot(water_temps.rolling(10, center=True, win_type='boxcar').mean())
    plt.gca().xaxis.set_major_formatter(DateFormatter(xtick_dt_fmt))
    plt.xticks(rotation=45)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.savefig(path, bbox_inches='tight', pad_inches=0.25)

    return path


def post_water_temps(channel: str, station: Optional[str] = None) -> None:
    station, water_temps = get_water_temps(station_id=station)
    plot_img_path = plot_temps_file(water_temps, station)
    observations_url = OBSERVATIONS_URL_TEMPLATE.format(id=station.id)
    post_txt = f"<{observations_url}|NOAA water temperature observations at {station.name} for the last 36 hours>\n"
    post_file(plot_img_path, title="Water temperature", channel=channel, comment=post_txt)


def _cleanup():
    for path in _cleanup_paths:
        os.remove(path)


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--station', type=str, default=THE_BATTERY_STATION_ID)
    parser.add_argument('--channel', type=str, default=NykpSlackChannels.test_python_api)
    return parser


def main(args):
    station, water_temps = get_water_temps(station_id=args.station)
    post_water_temps(args.channel, station, water_temps)
    _cleanup()


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

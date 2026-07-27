import itertools as it
import os
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime
from typing import Tuple, Union
from urllib.request import urlopen, urlretrieve

from bs4 import BeautifulSoup, Tag
import pytz

from utils.geo import LatLon
from utils.scripts import try_main
from utils.slack import NykpSlackChannels, post_message, post_file

URL_BASE = 'https://forecast.weather.gov/'
DEFAULT_LAT_LON = LatLon(40.7143, -74.006)
CACHE_DIR = 'cache'
IMG_FILENAME_TEMPLATE = 'nws_forecast_{lat}_{lon}_{ts}.png'
_cleanup_paths = []
_LatLonType = Union[LatLon, Tuple[float, float]]


def _is_forecast_img_tag(tag: Tag) -> bool:
    return tag.attrs['src'].startswith('meteograms/Plotter.php')


def save_forecast_plot(lat_lon: _LatLonType = DEFAULT_LAT_LON) -> str:
    if not isinstance(lat_lon, LatLon):
        lat_lon = LatLon(*lat_lon)
    page_url_pattern = ('MapClick.php?w0=t&w3=sfcwind&w3u=1&w4=sky&w5=pop&w6=rh&w7=rain&w8=thunder&AheadHour=0'
                        '&Submit=Submit&FcstType=graphical&textField1={lat}&textField2={lon}&site=all&unit=0&dd=&bw=')
    page_url = os.path.join(URL_BASE, page_url_pattern.format(lat=lat_lon.latitude, lon=lat_lon.longitude))
    page = urlopen(page_url)
    soup = BeautifulSoup(page, 'html.parser')
    img_tags = soup.find_all('img')
    filtered_tags = list(filter(_is_forecast_img_tag, img_tags))
    if len(filtered_tags) == 0:
        raise RuntimeError(f'Could not find forecast image url at {page_url}')
    elif len(filtered_tags) > 1:
        raise RuntimeError(f'Multiple candidate forecast image urls at {page_url}')
    img_url = URL_BASE + filtered_tags[0]['src']
    now_str = datetime.now(tz=pytz.UTC).strftime('%Y%m%dT%H%M%S')
    img_filename = IMG_FILENAME_TEMPLATE.format(lat=lat_lon.latitude, lon=lat_lon.longitude, ts=now_str)
    img_path = os.path.join(CACHE_DIR, img_filename)
    os.makedirs(os.path.dirname(img_path), exist_ok=True)
    path, resp = urlretrieve(img_url, img_path)
    return path


@dataclass
class ForecastText:
    title: str
    forecast: str


def get_forecast_text(lat_lon: _LatLonType = DEFAULT_LAT_LON) -> ForecastText:
    if not isinstance(lat_lon, LatLon):
        lat_lon = LatLon(*lat_lon)
    page_url_pattern = 'MapClick.php?lat={lat}&lon={lon}&unit=0&lg=english&FcstType=text&TextType=1'
    page_url = os.path.join(URL_BASE, page_url_pattern.format(lat=lat_lon.latitude, lon=lat_lon.longitude))
    page = urlopen(page_url)
    soup = BeautifulSoup(page, 'html.parser')
    title_tag, forecast_tag = soup.find_all('table')

    title_parts = list(title_tag.stripped_strings)
    title_str = f'{title_parts[0]}, {title_parts[3]}'

    num_relevant = 2
    forecast_parts = list(forecast_tag.stripped_strings)
    forecast_str_parts = []
    for i in range(num_relevant):
        period, forecast = forecast_parts[(2 * i):(2 * i + 2)]
        forecast_str_parts.append(f'{period} {forecast}')

    forecast_str = '\n\n'.join(forecast_str_parts)
    return ForecastText(title_str, forecast_str)



def post_forecast(channel: str, lat_lon: _LatLonType = DEFAULT_LAT_LON, text=True, plot=True, keep=False):
    forecast_text = get_forecast_text(lat_lon) if text else None
    img_path = save_forecast_plot(lat_lon) if plot else None
    if not keep:
        _cleanup_paths.append(img_path)
    if forecast_text:
        msg = f"*{forecast_text.title}*\n\n{forecast_text.forecast}"
    else:
        msg = None
    if img_path:
        post_file(img_path, channel=channel, comment=msg)
    elif msg:
        post_message(msg, channel=channel)


def _cleanup():
    for path in _cleanup_paths:
        os.remove(path)


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--channel', type=str, default=NykpSlackChannels.test_python_api)
    parser.add_argument('--lat', type=float, default=None)
    parser.add_argument('--lon', type=float, default=None)
    parser.add_argument('--keep', action='store_true')
    return parser


def main(args):
    if args.lat and not args.lon or args.lon and not args.lat:
        raise ValueError(f'Both latitude and longitude required if not using default location')
    if args.lat and args.lon:
        lat_lon = LatLon(latitude=args.lat, longitude=args.lon)
        post_forecast(args.channel, lat_lon=lat_lon, keep=args.keep)
    else:
        post_forecast(args.channel, keep=args.keep)
    _cleanup()


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

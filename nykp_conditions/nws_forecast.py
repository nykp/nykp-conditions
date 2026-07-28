"""National Weather Service forecast: hourly meteogram image and text summary.

Both come from forecast.weather.gov, which has no JSON API for these, so the
page is scraped: the meteogram is an <img> pointing at `meteograms/Plotter.php`,
and the text forecast is the first two periods of the tabular text page.
"""

import os
import tempfile
from argparse import ArgumentParser
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin
from urllib.request import urlopen, urlretrieve

from bs4 import BeautifulSoup, Tag

from utils.geo import LatLon
from utils.scripts import try_main
from utils.slack import NykpSlackChannels, post_message, post_file

URL_BASE = 'https://forecast.weather.gov/'
DEFAULT_LAT_LON = LatLon(40.7143, -74.006)
CACHE_DIR = 'cache'
IMG_FILENAME_TEMPLATE = 'nws_forecast_{lat}_{lon}_{ts}.png'
_LatLonType = LatLon | tuple[float, float]


def _as_lat_lon(lat_lon: _LatLonType) -> LatLon:
    """Accept either a LatLon or a plain (lat, lon) tuple."""
    return lat_lon if isinstance(lat_lon, LatLon) else LatLon(*lat_lon)


def _is_forecast_img_tag(tag: Tag) -> bool:
    """True for the <img> holding the meteogram plot."""
    return tag.attrs.get('src', '').startswith('meteograms/Plotter.php')


def find_forecast_img_url(lat_lon: _LatLonType = DEFAULT_LAT_LON) -> str:
    """Scrape the graphical forecast page and return the meteogram image URL.

    Raises RuntimeError if the page has no meteogram image or more than one,
    since either means the page layout changed and the scrape is unreliable.
    """
    lat_lon = _as_lat_lon(lat_lon)
    page_url_pattern = ('MapClick.php?w0=t&w3=sfcwind&w3u=1&w4=sky&w5=pop&w6=rh&w7=rain&w8=thunder&AheadHour=0'
                        '&Submit=Submit&FcstType=graphical&textField1={lat}&textField2={lon}&site=all&unit=0&dd=&bw=')
    page_url = urljoin(URL_BASE, page_url_pattern.format(lat=lat_lon.latitude, lon=lat_lon.longitude))
    with urlopen(page_url) as page:
        soup = BeautifulSoup(page, 'html.parser')
    filtered_tags = list(filter(_is_forecast_img_tag, soup.find_all('img')))
    if len(filtered_tags) == 0:
        raise RuntimeError(f'Could not find forecast image url at {page_url}')
    elif len(filtered_tags) > 1:
        raise RuntimeError(f'Multiple candidate forecast image urls at {page_url}')
    return urljoin(URL_BASE, filtered_tags[0]['src'])


def forecast_img_filename(lat_lon: _LatLonType = DEFAULT_LAT_LON, now: datetime | None = None) -> str:
    """Timestamped filename for a forecast image at this location."""
    lat_lon = _as_lat_lon(lat_lon)
    if now is None:
        now = datetime.now(tz=timezone.utc)
    return IMG_FILENAME_TEMPLATE.format(
        lat=lat_lon.latitude, lon=lat_lon.longitude, ts=now.strftime('%Y%m%dT%H%M%S')
    )


def save_forecast_plot(lat_lon: _LatLonType = DEFAULT_LAT_LON, path: str | None = None) -> str:
    """Download the meteogram image, returning the path it was written to.

    With no `path`, writes to a temp file the caller is responsible for
    removing; `post_forecast` supplies a path inside a managed directory.
    """
    img_url = find_forecast_img_url(lat_lon)
    if path is None:
        fd, path = tempfile.mkstemp(prefix='nws_forecast_', suffix='.png')
        os.close(fd)
    else:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    path, _ = urlretrieve(img_url, path)
    return path


@dataclass
class ForecastText:
    title: str
    forecast: str


def get_forecast_text(lat_lon: _LatLonType = DEFAULT_LAT_LON) -> ForecastText:
    """Scrape the text forecast page for the location title and next 2 periods."""
    lat_lon = _as_lat_lon(lat_lon)
    page_url_pattern = 'MapClick.php?lat={lat}&lon={lon}&unit=0&lg=english&FcstType=text&TextType=1'
    page_url = urljoin(URL_BASE, page_url_pattern.format(lat=lat_lon.latitude, lon=lat_lon.longitude))
    with urlopen(page_url) as page:
        soup = BeautifulSoup(page, 'html.parser')
    return parse_forecast_text(soup)


def parse_forecast_text(soup: BeautifulSoup, num_periods: int = 2) -> ForecastText:
    """Pull the title and first `num_periods` forecast periods out of the page.

    Split out from the fetch so the (fragile) scraping can be tested against
    saved HTML without hitting the network.
    """
    tables = soup.find_all('table')
    if len(tables) < 2:
        raise RuntimeError(f'Expected 2 tables in the text forecast page, found {len(tables)}')
    title_tag, forecast_tag = tables[:2]

    title_parts = list(title_tag.stripped_strings)
    if len(title_parts) < 4:
        raise RuntimeError(f'Unexpected forecast title layout: {title_parts}')
    title_str = f'{title_parts[0]}, {title_parts[3]}'

    # The forecast table alternates period name / forecast text.
    forecast_parts = list(forecast_tag.stripped_strings)
    forecast_str_parts = []
    for i in range(min(num_periods, len(forecast_parts) // 2)):
        period, forecast = forecast_parts[(2 * i):(2 * i + 2)]
        forecast_str_parts.append(f'{period} {forecast}')
    if not forecast_str_parts:
        raise RuntimeError('No forecast periods found in the text forecast page')

    return ForecastText(title_str, '\n\n'.join(forecast_str_parts))


@contextmanager
def _plot_dir(keep: bool) -> Iterator[str]:
    """Directory to download the plot into.

    When keeping the image, use the (gitignored) cache dir. Otherwise use a
    temp dir removed only once the upload has returned, so the file can't
    vanish mid-upload the way the old module-level cleanup allowed.
    """
    if keep:
        os.makedirs(CACHE_DIR, exist_ok=True)
        yield CACHE_DIR
    else:
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield tmp_dir


def post_forecast(
        channel: str, lat_lon: _LatLonType = DEFAULT_LAT_LON, text=True, plot=True, keep=False
) -> None:
    """Post the NWS forecast to Slack: meteogram image, text summary, or both."""
    if not (text or plot):
        raise ValueError('Nothing to post: at least one of text or plot must be requested')

    forecast_text = get_forecast_text(lat_lon) if text else None
    msg = f"*{forecast_text.title}*\n\n{forecast_text.forecast}" if forecast_text else None

    if not plot:
        post_message(msg, channel=channel)
        return

    with _plot_dir(keep) as plot_dir:
        img_path = save_forecast_plot(lat_lon, path=os.path.join(plot_dir, forecast_img_filename(lat_lon)))
        post_file(img_path, title="Forecast", channel=channel, comment=msg)


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--channel', type=str, default=NykpSlackChannels.test_python_api)
    parser.add_argument('--lat', type=float, default=None)
    parser.add_argument('--lon', type=float, default=None)
    parser.add_argument('--keep', action='store_true', help=f'Keep the downloaded plot in {CACHE_DIR}/')
    return parser


def main(args):
    if bool(args.lat is None) != bool(args.lon is None):
        raise ValueError('Both latitude and longitude required if not using default location')
    if args.lat is not None:
        post_forecast(args.channel, lat_lon=LatLon(latitude=args.lat, longitude=args.lon), keep=args.keep)
    else:
        post_forecast(args.channel, keep=args.keep)


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

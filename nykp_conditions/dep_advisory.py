"""Waterbody advisories from the NYC DEP Waterbody Advisory Dashboard.

`notify_nyc.py` reports advisories the way the city *announces* them: a prose
RSS item, published only when DEP decides to push one out. This module reads
the dashboard those announcements come from
(https://nycwaterbodyadvisory.azurewebsites.net/), which carries the current
status of all 45 waterbodies, hour by hour, whether or not anything was
announced.

## There is no map image to download

The dashboard's map is not a picture the server draws. It is an ArcGIS
JavaScript layer that ships vector features to the browser and paints them on
a canvas there, so nothing on that page is a URL we can fetch. Nor does the
layer behind it offer a rendered fallback: its FeatureServer advertises
`Query` but not `Map`, which means the ArcGIS `export` image operation is not
available on it, and the `f=image` responses a MapServer would give simply do
not exist here.

What the server will hand over is the geometry. So `plot_advisory_map` pulls
the waterbody polygons, joins them to the live status, and draws our own map.
That turns out better than a screenshot would have been: we can key the colors
to advisory status alone, outline the water we actually paddle on, and get an
image sized for Slack rather than for an 850px dashboard panel.

## The API

* `GET /api/advisory?advisoryType=WQ` -- one record per waterbody, giving the
  most recent rainfall hour (`occurredOn`), how many more hours the advisory
  runs from it (`duration`), the waterbody's DEC class, and the rain gauge the
  advisory is keyed to. A `duration` of 0 means no advisory.
* `GET /api/profile` -- dashboard config, including the ArcGIS layer URLs.
* `GET <layer>/query` -- the waterbody polygons. Requested generalized, which
  takes the response from 10 MB to about 140 KB with no visible difference at
  the size we plot.

Its `occurredOn` timestamps are naive and local to New York, not UTC. Reading
them as UTC would put the advisory window four or five hours off.

The dashboard also exposes hourly rainfall per gauge at
`/api/rainfall-samples?page=0&pageSize=96&sensorId=NR`, which is what its bar
chart draws. We don't use it: `nws_precip.py` already posts observed
precipitation. Note if you ever do that `accumulatedLast24Hours` on
`/api/sensors` is not to be trusted -- it read 0.0 for every gauge on
2026-08-21, hours after North River had recorded 2.55 in of rain.
"""

import json
import os
import tempfile
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import PatchCollection
from matplotlib.patches import Patch, Polygon

from utils.format import format_hour
from utils.scripts import try_main
from utils.slack import NykpSlackChannels, post_file, post_message

DASHBOARD_URL = 'https://nycwaterbodyadvisory.azurewebsites.net/'
API_BASE = 'https://nycwaterbodyadvisory.azurewebsites.net/api'
USER_AGENT = 'nykp-conditions (https://github.com/nykp/noaa-currents)'

DEFAULT_TIMEZONE = 'America/New_York'

# We paddle off Pier 96, which is Hudson River (Manhattan) on DEP's map.
HOME_WATERBODY = 'Hudson River (Manhattan)'

# Water quality advisories, as opposed to 'CSO' (combined sewer overflow
# discharge volumes). The two dashboard tabs share the /api/advisory shape.
WQ = 'WQ'

# Simplification tolerance in degrees, ~20 m. The polygons are coastline-
# accurate and enormous at full resolution; at the scale we plot, a pixel is
# roughly 80 m, so this is well below anything that could show.
GEOMETRY_TOLERANCE = 0.0002
# Bounds of the layer, padded slightly. Fixed rather than derived from the
# geometry so the map frames identically from day to day.
MAP_BOUNDS = (-74.28, -73.72, 40.47, 40.93)  # west, east, south, north

ADVISORY_COLOR = '#d94a3d'
CLEAR_COLOR = '#5b8db8'
LAND_COLOR = '#f2efe9'

# DEC use classifications, as the dashboard's own class tooltips word them.
WATER_CLASSES = {
    'SA': 'Best usages are shellfishing for market purposes, primary and secondary contact recreation and fishing',
    'SB': 'Best usages are primary and secondary contact recreation and fishing',
    'SB(WW)': 'Best usages of Class SB waters with limited suspension following qualifying rain events',
    'SC': 'Best usage is fishing. Factors other than water quality limit recreational uses.',
    'SD': 'Best usage is fishing. Natural or man-made conditions limit fish propagation.',
    'I': 'Best usages are secondary contact recreation and fishing',
}


def _get_json(url: str, **params):
    """Fetch and decode a JSON response."""
    if params:
        url = f'{url}?{urlencode(params)}'
    request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urlopen(request) as resp:
        return json.load(resp)


def _localize(timestamp: str, tz: str = DEFAULT_TIMEZONE) -> datetime:
    """Read one of the API's naive local timestamps as a New York datetime."""
    return datetime.fromisoformat(timestamp).replace(tzinfo=ZoneInfo(tz))


@dataclass
class WaterbodyAdvisory:
    """One waterbody's advisory status as of the latest rainfall hour."""

    waterbody: str
    waterbody_id: int
    water_class: str
    rain_gauge: str | None
    occurred_on: datetime  # Latest hour of rainfall the status accounts for
    duration_hours: float  # Hours of advisory remaining from `occurred_on`
    message: str

    @property
    def active(self) -> bool:
        return self.duration_hours > 0

    @property
    def until(self) -> datetime:
        """When the advisory lifts. Matches the API's own `message` text."""
        return self.occurred_on + timedelta(hours=self.duration_hours)

    @property
    def class_description(self) -> str | None:
        return WATER_CLASSES.get(self.water_class)

    @classmethod
    def parse(cls, record: dict, tz: str = DEFAULT_TIMEZONE) -> 'WaterbodyAdvisory':
        waterbody = record['waterbody']
        return cls(
            waterbody=waterbody['name'],
            waterbody_id=waterbody['id'],
            water_class=waterbody['class'],
            rain_gauge=waterbody.get('rainGauge'),
            occurred_on=_localize(record['occurredOn'], tz=tz),
            duration_hours=float(record['duration']),
            message=record['message'],
        )


def get_advisories(advisory_type: str = WQ, tz: str = DEFAULT_TIMEZONE) -> list[WaterbodyAdvisory]:
    """Current advisory status for every waterbody on the dashboard."""
    records = _get_json(f'{API_BASE}/advisory', advisoryType=advisory_type)
    return [WaterbodyAdvisory.parse(record, tz=tz) for record in records]


def find_advisory(
        advisories: list[WaterbodyAdvisory], waterbody: str = HOME_WATERBODY
) -> WaterbodyAdvisory | None:
    """Pick one waterbody out of the dashboard's list by name."""
    for advisory in advisories:
        if advisory.waterbody == waterbody:
            return advisory
    return None


def get_layer_url(advisory_type: str = WQ) -> str:
    """The ArcGIS layer holding the waterbody polygons, per the dashboard config.

    Read from `/api/profile` rather than hardcoded: the dashboard points at a
    differently-named layer per environment, and following its own config means
    we move when it does.
    """
    profile = _get_json(f'{API_BASE}/profile')
    key = f'{advisory_type.lower()}.arcgis.feature_layer.url'
    try:
        return profile[key]
    except KeyError:
        raise RuntimeError(f"Dashboard profile has no {key!r}; keys: {sorted(profile)}") from None


def get_waterbody_shapes(
        layer_url: str | None = None, tolerance: float = GEOMETRY_TOLERANCE
) -> dict[int, list[np.ndarray]]:
    """Waterbody outlines as lon/lat rings, keyed by the layer's FID.

    FID matches the `waterbody.id` on `/api/advisory` exactly -- all 45 ids and
    names line up -- so that's the join between shape and status.
    """
    if layer_url is None:
        layer_url = get_layer_url()
    features = _get_json(
        urljoin(layer_url, 'query'),
        where='1=1',
        outFields='FID',
        returnGeometry='true',
        outSR=4326,
        maxAllowableOffset=tolerance,
        f='geojson',
    )['features']

    shapes = {}
    for feature in features:
        geometry = feature['geometry']
        # A waterbody may be a Polygon or, where it wraps an island, a
        # MultiPolygon; either way we only need the rings to draw it.
        if geometry['type'] == 'Polygon':
            rings = geometry['coordinates']
        else:
            rings = [ring for polygon in geometry['coordinates'] for ring in polygon]
        shapes[feature['properties']['FID']] = [np.asarray(ring)[:, :2] for ring in rings]
    return shapes


def plot_advisory_map(
        advisories: list[WaterbodyAdvisory],
        shapes: dict[int, list[np.ndarray]],
        highlight: str | None = HOME_WATERBODY,
        path: str | None = None,
) -> str:
    """Draw the advisory map to a PNG, returning the path written.

    Our stand-in for the dashboard's map, which can't be fetched as an image
    (see the module docstring). Waterbodies on advisory are filled red, the
    rest blue, and `highlight` is outlined and labelled.
    """
    if path is None:
        fd, path = tempfile.mkstemp(prefix='dep_advisory_map_', suffix='.png')
        os.close(fd)

    west, east, south, north = MAP_BOUNDS
    as_of = max(a.occurred_on for a in advisories)
    active = [a for a in advisories if a.active]

    fig, ax = plt.subplots(figsize=(8, 8))
    try:
        by_status = {True: [], False: []}
        for advisory in advisories:
            for ring in shapes.get(advisory.waterbody_id, []):
                by_status[advisory.active].append(Polygon(ring, closed=True))

        for is_active, color in ((False, CLEAR_COLOR), (True, ADVISORY_COLOR)):
            ax.add_collection(PatchCollection(
                by_status[is_active], facecolor=color, edgecolor=color, linewidth=0.3
            ))

        highlighted = find_advisory(advisories, highlight) if highlight else None
        if highlighted is not None:
            for ring in shapes.get(highlighted.waterbody_id, []):
                ax.add_patch(Polygon(ring, closed=True, fill=False,
                                     edgecolor='#111111', linewidth=1.4))
            # Label to the left of the waterbody, pointing at its middle.
            centroid = np.concatenate(shapes[highlighted.waterbody_id]).mean(axis=0)
            ax.annotate(
                highlighted.waterbody,
                xy=centroid, xytext=(west + 0.02, centroid[1]),
                fontsize=9, fontweight='bold', va='center', ha='left',
                arrowprops={'arrowstyle': '-', 'linewidth': 1.0, 'color': '#111111'},
            )

        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        # Degrees of longitude are shorter than degrees of latitude away from
        # the equator; without this the city comes out stretched east-west.
        ax.set_aspect(1 / np.cos(np.radians((south + north) / 2)))
        ax.set_facecolor(LAND_COLOR)
        ax.set_xticks([])
        ax.set_yticks([])

        ax.legend(
            handles=[Patch(facecolor=ADVISORY_COLOR, label='On advisory'),
                     Patch(facecolor=CLEAR_COLOR, label='No advisory')],
            loc='lower left', framealpha=0.9, fontsize=9,
        )
        ax.set_title(f'NYC DEP water quality advisories, {format_hour(as_of)}\n'
                     f'{len(active)} of {len(advisories)} waterbodies on advisory',
                     fontsize=11)

        fig.savefig(path, bbox_inches='tight', pad_inches=0.2, dpi=110)
    finally:
        plt.close(fig)

    return path


def format_advisory_post(
        advisory: WaterbodyAdvisory, advisories: list[WaterbodyAdvisory] | None = None
) -> str:
    """Build the Slack message body for one waterbody's advisory."""
    header = f"<{DASHBOARD_URL}|DEP waterbody advisory>: *{advisory.waterbody}*"
    if not advisory.active:
        return f"{header}\n*No advisory.*"

    lines = [
        header,
        f"*On advisory until {format_hour(advisory.until)}* "
        f"({advisory.duration_hours:.0f} more hours as of {format_hour(advisory.occurred_on)})",
    ]
    if advisory.class_description:
        lines.append(f"Class {advisory.water_class}: {advisory.class_description}")
    if advisories:
        active = sum(1 for a in advisories if a.active)
        lines.append(f"_{active} of {len(advisories)} NYC waterbodies are on advisory._")
    return '\n'.join(lines)


def post_waterbody_advisory(
        channel: str,
        waterbody: str = HOME_WATERBODY,
        post_when_clear: bool = False,
) -> None:
    """Post the waterbody's advisory status to Slack, with a map of the city's.

    Quiet by default when there's no advisory: a standing "no advisory" line
    would be noise on the great majority of days, and the map is only worth
    looking at when something on it is red.
    """
    advisories = get_advisories()
    advisory = find_advisory(advisories, waterbody=waterbody)
    if advisory is None:
        known = ', '.join(sorted(a.waterbody for a in advisories))
        raise ValueError(f"No waterbody named {waterbody!r} on the DEP dashboard. Known: {known}")

    print(f"DEP advisory for {advisory.waterbody}: {advisory.message}")
    if not advisory.active:
        if post_when_clear:
            post_message(format_advisory_post(advisory), channel=channel, unfurl_links=False)
        return

    post_text = format_advisory_post(advisory, advisories)
    # Keep the map in a temp dir that's cleaned up only after the upload
    # returns (success or failure), so the file can't vanish mid-upload.
    with tempfile.TemporaryDirectory() as tmp_dir:
        map_path = os.path.join(tmp_dir, 'dep_advisory_map.png')
        plot_advisory_map(advisories, get_waterbody_shapes(), highlight=waterbody, path=map_path)
        post_file(map_path, title='DEP waterbody advisories',
                  channel=channel, comment=post_text)


"""----------------------------------------------------------------------------
SCRIPT CODE
----------------------------------------------------------------------------"""


def parse_args():
    parser = ArgumentParser(description='Post NYC DEP waterbody advisory status to Slack.')
    parser.add_argument('--channel', type=str, default=NykpSlackChannels.test_python_api)
    parser.add_argument('--waterbody', type=str, default=HOME_WATERBODY)
    parser.add_argument('--post-when-clear', action='store_true',
                        help='Post a "no advisory" line instead of staying quiet')
    return parser


def main(args):
    post_waterbody_advisory(args.channel, waterbody=args.waterbody,
                            post_when_clear=args.post_when_clear)


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

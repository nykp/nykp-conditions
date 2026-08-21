#!/usr/bin/env python3

"""Post the daily conditions summary to Slack.

Entry point for the scheduled GitHub Actions run. Each section is posted
independently: one upstream source being down shouldn't cost us the rest of
the morning's post, so failures are collected and reported at the end, with a
nonzero exit so the workflow run still shows as failed.
"""

import sys
import traceback
from argparse import ArgumentParser
from collections.abc import Callable

from dep_advisory import post_waterbody_advisory
from noaa_currents import post_currents
from notify_nyc import post_waterbody_advisories
from nws_forecast import post_forecast
from nws_precip import post_observed_precip
from utils.scripts import str2bool, try_main
from utils.slack import NykpSlackChannels
from water_temps import post_water_temps


default_config = {
    'currents': True,
    'advisories': True,
    'dep-advisory': True,
    'precip': True,
    'water-temp': True,
    'forecast': True,
}


def _add_config_fields(parser: ArgumentParser):
    for field, default in default_config.items():
        parser.add_argument(f'--{field}', type=str2bool, default=default,
                            help=f'Post {field} (default: {default})')
    return parser


def post_sections(sections: list[tuple[str, Callable[[], None]]]) -> list[str]:
    """Run each (name, post-callable) in order, returning the names that failed.

    Errors are printed with a traceback rather than raised, so a failure in one
    section doesn't prevent the others from posting.
    """
    failed = []
    for name, post in sections:
        try:
            post()
        except Exception:
            failed.append(name)
            print(f"ERROR posting {name}:", file=sys.stderr)
            traceback.print_exc()
    return failed


def parse_args() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--channel', type=str, default=None)
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--water-temp-plot', type=str2bool, default=False,
                        help='Attach the water temperature timeseries plot (default: False)')
    parser = _add_config_fields(parser)
    return parser


def main(args):
    channel = args.channel if args.channel is not None else NykpSlackChannels.test_python_api

    sections = []
    if args.currents:
        sections.append(('currents', lambda: post_currents(channel, days=args.days)))
    if args.water_temp:
        sections.append(('water temperature', lambda: post_water_temps(channel, plot=args.water_temp_plot)))
    if args.forecast:
        sections.append(('forecast', lambda: post_forecast(channel)))
    if args.precip:
        sections.append(('precip', lambda: post_observed_precip(channel)))
    if args.advisories:
        sections.append(('advisories', lambda: post_waterbody_advisories(channel, days=args.days)))
    if args.dep_advisory:
        sections.append(('DEP advisory', lambda: post_waterbody_advisory(channel)))

    failed = post_sections(sections)
    if failed:
        raise RuntimeError(f"Failed to post {len(failed)} of {len(sections)} sections: {', '.join(failed)}")


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

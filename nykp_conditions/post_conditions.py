#!/usr/bin/env python3

from argparse import ArgumentParser

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
    'precip': True,
    'water-temp': True,
    'forecast': True,
}


def _add_config_fields(parser: ArgumentParser):
    for field, default in default_config.items():
        parser.add_argument(f'--{field}', type=str2bool, default=default,
                            help=f'Post {field} (default: {default})')
    return parser


def parse_args() -> ArgumentParser:
    parser = ArgumentParser()
    parser.add_argument('--channel', type=str, default=None)
    parser.add_argument('--days', type=int, default=1)
    parser = _add_config_fields(parser)
    return parser


def main(args):
    if args.channel is not None:
        channel = args.channel
    else:
        channel = NykpSlackChannels.test_python_api
    if args.currents:
        post_currents(channel, days=args.days)
    if args.advisories:
        post_waterbody_advisories(channel, days=args.days)
    if args.precip:
        post_observed_precip(channel)
    if args.water_temp:
        post_water_temps(channel)
    if args.forecast:
        post_forecast(channel)


if __name__ == '__main__':
    parser = parse_args()
    try_main(main, parser)

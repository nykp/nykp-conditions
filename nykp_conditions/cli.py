#!/usr/bin/env python3

"""The `nykp-conditions` command.

Installing the package puts a single `nykp-conditions` on the PATH, with one
subcommand per runnable module:

    nykp-conditions post --channel hudson-conditions
    nykp-conditions web-export --out data/conditions.json
    nykp-conditions currents --station NYH1928_12 --range 1w

Each subcommand hands off to the module's own `parse_args`/`main`, so there is
exactly one definition of every flag and the dispatcher can't drift from it.
`python -m nykp_conditions.<module>` keeps working unchanged for anyone who
prefers it, and `python -m nykp_conditions` reaches this dispatcher.
"""

import sys
from collections.abc import Sequence

from . import (
    dep_advisory,
    noaa_currents,
    notify_nyc,
    nws_forecast,
    nws_precip,
    post_conditions,
    water_temps,
    web_export,
)
from .utils.scripts import try_main

PROG = 'nykp-conditions'

# Subcommand -> (module, one-line description). Ordered as the help lists them:
# the two whole-summary commands first, then the individual sources.
COMMANDS = {
    'post': (post_conditions, 'Post the full conditions summary to Slack'),
    'web-export': (web_export, 'Write the conditions as JSON + plots for the website'),
    'currents': (noaa_currents, 'Tidal current predictions'),
    'water-temps': (water_temps, 'Water temperature range'),
    'forecast': (nws_forecast, 'NWS text forecast and meteogram'),
    'precip': (nws_precip, 'Observed precipitation'),
    'notify-nyc': (notify_nyc, 'Waterbody advisories announced through Notify NYC'),
    'dep-advisory': (dep_advisory, 'DEP waterbody advisory status and citywide map'),
}


def format_usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [
        f'usage: {PROG} <command> [options]',
        '',
        'Commands:',
    ]
    lines += [f'  {name:<{width}}  {description}'
              for name, (_, description) in COMMANDS.items()]
    lines += [
        '',
        f'Run `{PROG} <command> --help` for a command\'s own options.',
    ]
    return '\n'.join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(format_usage())
        return 0

    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"{PROG}: unknown command {name!r}\n", file=sys.stderr)
        print(format_usage(), file=sys.stderr)
        return 2

    module, _ = COMMANDS[name]
    parser = module.parse_args()
    # So `nykp-conditions post --help` reports itself rather than the module.
    parser.prog = f'{PROG} {name}'
    try_main(module.main, parser, argv=rest)
    return 0


if __name__ == '__main__':
    sys.exit(main())

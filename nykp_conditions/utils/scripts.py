"""Shared CLI plumbing for the runnable modules."""

import pdb
import traceback
from argparse import ArgumentParser
from typing import Callable, Type, Union


def str2bool(s: Union[bool, str, None], ignore_errors=False) -> Union[bool, str, None]:
    """Parse a boolean from a CLI string ('true'/'no'/'1'/...).

    argparse's `type=bool` treats any non-empty string as True, so flags like
    `--currents false` need this instead.
    """
    try:
        if isinstance(s, bool) or s is None:
            return s
        if isinstance(s, str):
            if s.lower() in ('true', 't', 'yes', 'y', '1', 'on'):
                return True
            if s.lower() in ('false', 'f', 'no', 'n', '0', 'off'):
                return False
            raise ValueError('Ambiguous value, cannot convert to boolean')
        raise TypeError
    except (ValueError, TypeError):
        if ignore_errors:
            return s
        else:
            raise


def optional(type_: Type):
    """argparse type that maps 'none'/'null' to None and otherwise converts."""
    def _converter(x):
        if x is None or x.lower() in ('none', 'null'):
            return None
        else:
            return type_(x)
    return _converter


def try_main(main: Callable, arg_parser: ArgumentParser):
    """Parse args and run `main`, with optional pdb entry and post-mortem."""
    arg_parser.add_argument('--pdb', action='store_true')
    arg_parser.add_argument('--postmortem', action='store_true')
    args = arg_parser.parse_args()

    if args.pdb:
        pdb.set_trace()

    try:
        main(args)
    except (KeyboardInterrupt, pdb.bdb.BdbQuit):
        pass
    except Exception:
        if args.postmortem:
            traceback.print_exc()
            pdb.post_mortem()
        else:
            raise

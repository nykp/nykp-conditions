"""So `python -m nykp_conditions` reaches the same dispatcher as the
`nykp-conditions` console script."""

import sys

from .cli import main

if __name__ == '__main__':
    sys.exit(main())

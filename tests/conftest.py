"""Test setup.

`nykp_conditions` is a real package, so the tests import it by its full path
(`import nykp_conditions.noaa_currents as nc`) the same way any other consumer
would — no `sys.path` manipulation needed beyond running pytest from the repo
root, which `testpaths` in pyproject.toml already assumes.
"""

import os

os.environ.setdefault('MPLBACKEND', 'Agg')  # No GUI backend in CI

"""Test setup.

The modules under `nykp_conditions/` import each other flatly (`from utils.slack
import ...`) because they're run as scripts from inside that directory, so the
package dir itself has to be on `sys.path` for the tests to import them the same
way the scripts do.
"""

import os
import sys

os.environ.setdefault('MPLBACKEND', 'Agg')  # No GUI backend in CI

PACKAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, 'nykp_conditions'))
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

"""Unit conversions."""

from typing import Optional

import numpy as np

KNOTS_TO_MPH = 1.15078


def knots_to_mph(knots) -> Optional[float]:
    """Convert knots to mph, returning NaN for values that aren't numeric.

    NOAA CSVs use blank cells for missing readings, so unparseable values
    become NaN rather than raising mid-table.
    """
    try:
        if isinstance(knots, str):
            knots = float(knots)
        return knots * KNOTS_TO_MPH
    except (TypeError, ValueError):
        return np.nan

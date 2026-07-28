"""Observed precipitation from NWS station observations. NOT IMPLEMENTED.

`post_conditions.py` calls `post_observed_precip` on every run, so it reports
that it's unimplemented and returns rather than failing the whole daily post.
The previous half-written implementation was removed: it never ran (the
retrieval function had an unformatted URL and passed a path to `json.load`),
and its exploratory version lives in `notebooks/precip_rate_series_dev_1.ipynb`.

To finish this: fetch https://api.weather.gov/stations/{station}/observations
for the window, pull `precipitationLastHour` out of each feature's properties,
and aggregate to hourly and 6-hour totals.
"""

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

DEFAULT_STATION = 'KNYC'
DEFAULT_TIMEZONE = 'America/New_York'
DATETIME_FMT = '%Y-%m-%dT%H:%M:%SZ'


def get_observations_url(station: str, start_dt: datetime, end_dt: datetime) -> str:
    """URL for a station's observations over a time window (UTC)."""
    start = start_dt.astimezone(timezone.utc).strftime(DATETIME_FMT)
    end = end_dt.astimezone(timezone.utc).strftime(DATETIME_FMT)
    return f'https://api.weather.gov/stations/{station}/observations?start={start}&end={end}'


def get_observed_precip(
        station: str = DEFAULT_STATION,
        as_of: Optional[datetime] = None,
        hours: int = 24,
) -> pd.Series:
    """Observed precipitation over the last `hours`. Not implemented yet."""
    raise NotImplementedError('Retrieving observed precipitation is not implemented yet')


def post_observed_precip(channel: str) -> None:
    """No-op placeholder so the daily post can include precipitation later."""
    print('Skipping precipitation: nws_precip.post_observed_precip is not implemented yet')

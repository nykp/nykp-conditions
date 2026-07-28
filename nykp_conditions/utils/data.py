"""Small DataFrame helpers."""

import pandas as pd


def rename_cols(df: pd.DataFrame, rename_map: dict) -> pd.DataFrame:
    """Rename columns matched by substring.

    NOAA's CSV headers carry units that change between endpoints (e.g.
    'Speed (knots)'), so columns are matched on a substring of the header
    rather than the exact name. Raises if a key matches no column or more
    than one, since either means the response format moved.
    """
    corrected_map = {}
    for col, new in rename_map.items():
        candidate_cols = [c for c in df if col in c]
        if len(candidate_cols) == 0:
            raise KeyError(f"No column matching {col!r} in columns: {list(df.columns)}")
        if len(candidate_cols) > 1:
            raise ValueError(f"Multiple matches for column {col!r}: {candidate_cols}")
        corrected_map[candidate_cols[0]] = new
    return df.rename(columns=corrected_map)

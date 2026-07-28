"""Plotting helpers shared by the chart-producing sources."""

import matplotlib as mpl
import matplotlib.pyplot as plt


class DateTimeFormats:
    # https://stackoverflow.com/a/47541788
    y = '%Y'
    y_m = '%Y-%m'
    y_m_d = '%Y-%m-%d'
    h_m_s = '%H:%M:%S'


def format_time_axis(ax=None, fmt=DateTimeFormats.h_m_s, axis=0):
    """Apply a datetime tick format to the x (0) or y (1) axis."""
    if ax is None:
        ax = plt.gca()
    if axis in (0, 'x', 'X'):
        axis = 0
    elif axis in (1, 'y', 'Y'):
        axis = 1
    else:
        raise ValueError(f'Unrecognized axis value: {axis}')
    if axis == 0:
        ax.xaxis.set_major_formatter(mpl.dates.DateFormatter(fmt))
    else:
        ax.yaxis.set_major_formatter(mpl.dates.DateFormatter(fmt))

"""Geographic types shared across the conditions sources."""

from dataclasses import dataclass


@dataclass
class LatLon:
    latitude: float
    longitude: float

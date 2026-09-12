"""Pinned, attributed historical observations; no live telemetry is implied."""

import json
from importlib.resources import files

PACKETS = json.loads(files("watershed_memory").joinpath("data/observations.json").read_text())
LABELS = ("July 9", "August 3", "September 10")
HEADLINES = ("The first evidence arrives", "The work carries forward", "An evidence gap emerges")
SOURCES = [
    {"label": "Gallinas field archive · CC BY 4.0", "url": "https://doi.org/10.5281/zenodo.12762324"},
    {"label": "USGS gauge 08380500", "url": "https://waterdata.usgs.gov/monitoring-location/USGS-08380500/"},
]


def event_cards(processed: int) -> list[dict]:
    """Expose narrative dates, but withhold measurements until their replay window arrives."""
    return [
        {
            "id": packet["event_id"],
            "date": packet["summary"]["date"],
            "label": LABELS[index],
            "headline": HEADLINES[index],
            "processed": index < processed,
            "p1_count": packet["p1_turbidity_count"] if index < processed else None,
            "p2_count": packet["p2_turbidity_count"] if index < processed else None,
            "flow_peak_cfs": (
                packet["summary"]["flow_usgs_cfs"]["day"]["maximum"]
                if index < processed else None
            ),
        }
        for index, packet in enumerate(PACKETS)
    ]

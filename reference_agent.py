"""
Minimal reference agent that passes the REIT7820 handshake test.

It is deliberately tiny: one domain tool over a hard-coded list of three
chargers. It shows the *shape* a conformant agent needs, not a thesis-worthy
tool. Run it, then point the handshake test at it:

    python reference_agent.py
    python handshake.py http://localhost:8000/mcp
"""
import math
from typing import Annotated

from pydantic import Field
from mcp.server.fastmcp import FastMCP

AGENT_NAME = "smac-charging-reference"   # smac-{domain}-{slot}
AGENT_VERSION = "1.0.0"                  # semantic version
DATA_SOURCES = ["Example charger list (hard-coded for this reference)"]
LLM_BACKEND = "none (rule-based reference)"

mcp = FastMCP(AGENT_NAME, host="127.0.0.1", port=8000)
mcp._mcp_server.version = AGENT_VERSION  # FastMCP has no version argument; set it here

CHARGERS = [
    {"name": "UQ St Lucia P10", "lat": -27.4969, "lon": 153.0145, "kw": 50},
    {"name": "Toowong Village", "lat": -27.4848, "lon": 152.9927, "kw": 75},
    {"name": "Indooroopilly Shopping Centre", "lat": -27.4990, "lon": 152.9730, "kw": 150},
]


def metres_between(lat1, lon1, lat2, lon2) -> int:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


@mcp.tool()
def health() -> dict:
    """Liveness check. Returns agent identity, data sources and model backend."""
    return {
        "status": "ok",
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "data_sources": DATA_SOURCES,
        "llm_backend": LLM_BACKEND,
    }


@mcp.tool()
def find_chargers_within_radius(
    lat: Annotated[float, Field(description="Latitude of the search centre, WGS84 decimal degrees", ge=-90, le=90)],
    lon: Annotated[float, Field(description="Longitude of the search centre, WGS84 decimal degrees", ge=-180, le=180)],
    radius_m: Annotated[int, Field(description="Search radius in metres", gt=0, le=50_000)] = 5000,
) -> dict:
    """Find public EV chargers in South East Queensland within a radius of a point,
    sorted by distance, with each charger's rated power in kW."""
    hits = []
    for c in CHARGERS:
        d = metres_between(lat, lon, c["lat"], c["lon"])
        if d <= radius_m:
            hits.append({"name": c["name"], "location": {"lat": c["lat"], "lon": c["lon"]},
                         "distance_m": d, "power_kw": c["kw"]})
    hits.sort(key=lambda h: h["distance_m"])
    return {"chargers": hits, "sources": DATA_SOURCES}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

#!/usr/bin/env python3
"""Poll SolarWinds SWIS for node positions and push a live JSON snapshot outbound.

This script bridges an internal-only SWIS/Orion server to a public static PWA
(OSH-A-ORG-2) without ever exposing SWIS credentials to a browser. It runs on
Sean's LAN, where SWIS is reachable, and pushes a compact array of resolved
positions to a Cloudflare Pages Function over HTTPS with a bearer token. It
does not write anything back to SWIS and it does not persist positions beyond
one in-memory poll cycle plus the small geocode cache described below.

Position resolution, per node, in priority order (see README.md "Priority
chain" for why this order and why City is last):

  1. A configured GPS custom property (SWIS_GPS_CUSTOM_PROPERTIES), if it is
     defined on this install (confirmed against Orion.CustomProperty at
     startup, not assumed) and its value parses as "lat,lon".
  2. Orion.Nodes.Location (the free-text sysLocation field), if it parses as
     "lat,lon".
  3. The City custom property, geocoded through OpenStreetMap Nominatim and
     cached in memory and on disk. This is last because City is a place name,
     not coordinates, so the result is a city centroid, not the asset's real
     position, and because whether City is a genuine default column on every
     SolarWinds install, rather than something this particular account
     happened to populate, is not settled by the extracted schema in
     SolarWinds_OrionGuides. See README.md for exactly what is and is not
     verified there.

Run with --dry-run to print the resolved snapshot instead of pushing it (this
also limits the run to a single poll cycle). See README.md for every
environment variable, the SWIS account rights this needs (read only, no
special rights), and how to re-validate the queries/*.swql files this script
is built from against a real SolarWinds_OrionGuides checkout.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from swis_client import Swis, SwisError, SwisPermanentError

LOG = logging.getLogger("swis-live-poller")

# SolarWinds' Worldwide Map geocoder accepts "lat,lon" typed into Location or a custom
# property. This regex is this script's reimplementation of that format: optional leading
# minus, digits, optional decimal portion, a comma, then the same shape for longitude, with
# no extra whitespace. It has not been checked against SolarWinds' own geocoder source, only
# against how populated Location values look in practice; sanity-check it against a real
# populated node before relying on it.
LAT_LON_RE = re.compile(r"^(-?\d{1,3}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)$")

ENTITIES_NEEDED = ["Orion.Nodes", "Orion.StatusInfo"]
ENTITIES_OPTIONAL = ["Orion.NodesCustomProperties", "Orion.CustomProperty"]

QUERY_NODES = """
SELECT n.NodeID, n.Caption, n.Location, n.Status, s.StatusName
FROM Orion.Nodes n
JOIN Orion.StatusInfo s ON s.StatusId = n.Status
ORDER BY n.NodeID
"""

QUERY_DISCOVER_CUSTOM_PROPERTIES = """
SELECT cp.Field, cp.DataType, cp.MaxLength
FROM Orion.CustomProperty cp
WHERE cp.Table = @table
ORDER BY cp.Field
"""

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0  # Nominatim usage policy: at most one request/second.
DEFAULT_GEOCODE_CACHE_PATH = "geocode_cache.json"


def build_custom_property_query(property_name: str) -> str:
    """Build a SELECT for one custom property's populated values, by node.

    Property names cannot be bound as SWQL parameters; parameters are values, not
    identifiers (SolarWinds_OrionGuides building-integrations.md section 5). So the name is
    formatted into the query text here, but only after names have been confirmed against
    Orion.CustomProperty, which is schema data read from the server itself, not caller- or
    config-supplied text reaching this point unchecked. _safe_identifier() below is a second,
    cheap gate against the same risk.
    """
    column = _safe_identifier(property_name)
    return f"""
SELECT n.NodeID, n.CustomProperties.{column} AS PropValue
FROM Orion.Nodes n
WHERE n.CustomProperties.{column} IS NOT NULL
"""


def _safe_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"refusing to use non-identifier custom property name: {name!r}")
    return name


def preflight(swis: Swis) -> set[str]:
    """Confirm the entities this script depends on exist on this server.

    Feature-detect rather than version-check, per building-integrations.md section 9.
    Raises SystemExit if a required entity is absent; logs and continues if an optional one
    is (the custom-property or city path is simply disabled for this run).
    """
    wanted = ENTITIES_NEEDED + ENTITIES_OPTIONAL
    rows = swis.query(
        "SELECT FullName FROM Metadata.Entity WHERE FullName IN @entities ORDER BY FullName",
        entities=wanted,
    )
    present = {row["FullName"] for row in rows}
    missing = [name for name in ENTITIES_NEEDED if name not in present]
    if missing:
        raise SystemExit(f"required entities absent from this server: {', '.join(missing)}")
    for name in ENTITIES_OPTIONAL:
        if name not in present:
            LOG.warning("optional entity %s absent; that part of the poll is disabled", name)
    return present


def discover_custom_properties(swis: Swis, present: set[str]) -> set[str]:
    """Return every custom property field name actually defined on Orion.Nodes.

    Used to validate SWIS_GPS_CUSTOM_PROPERTIES and SWIS_CITY_CUSTOM_PROPERTY before either
    is formatted into a query, so a stale or misspelled name in config is skipped with a
    warning instead of producing a runtime query error.
    """
    if "Orion.NodesCustomProperties" not in present or "Orion.CustomProperty" not in present:
        return set()
    rows = swis.query(QUERY_DISCOVER_CUSTOM_PROPERTIES, table="NodesCustomProperties")
    return {row["Field"] for row in rows}


def parse_lat_lon(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    match = LAT_LON_RE.match(value.strip())
    if not match:
        return None
    try:
        lat, lon = float(match.group(1)), float(match.group(2))
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


class Geocoder:
    """Nominatim City -> (lat, lon) lookup, rate-limited to Nominatim's usage policy and
    cached both in memory and to a small local JSON file so the cache survives a restart.
    See https://operations.osmfoundation.org/policies/nominatim/ : max one request per
    second, a descriptive User-Agent, and caching results instead of re-querying.
    """

    def __init__(self, cache_path: Path, user_agent: str) -> None:
        self.cache_path = cache_path
        self.user_agent = user_agent
        self._cache: dict[str, list[float] | None] = self._load()
        self._last_request = 0.0

    def _load(self) -> dict[str, list[float] | None]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.warning("could not read geocode cache at %s; starting empty", self.cache_path)
            return {}

    def _save(self) -> None:
        try:
            self.cache_path.write_text(
                json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            LOG.warning("could not write geocode cache: %s", exc)

    def geocode(self, place: str) -> tuple[float, float] | None:
        place = place.strip()
        if not place:
            return None
        if place in self._cache:
            cached = self._cache[place]
            return (cached[0], cached[1]) if cached else None

        elapsed = time.monotonic() - self._last_request
        if elapsed < NOMINATIM_MIN_INTERVAL_SECONDS:
            time.sleep(NOMINATIM_MIN_INTERVAL_SECONDS - elapsed)
        result = self._query(place)
        self._last_request = time.monotonic()

        self._cache[place] = list(result) if result else None
        self._save()
        return result

    def _query(self, place: str) -> tuple[float, float] | None:
        url = f"{NOMINATIM_URL}?{urlencode({'q': place, 'format': 'json', 'limit': 1})}"
        try:
            resp = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("nominatim lookup failed for %r: %s", place, exc)
            return None
        if not data:
            return None
        try:
            return float(data[0]["lat"]), float(data[0]["lon"])
        except (KeyError, ValueError, IndexError, TypeError):
            return None


def poll_snapshot(
    swis: Swis,
    gps_property_names: list[str],
    city_property_name: str | None,
    geocoder: Geocoder | None,
) -> list[dict[str, Any]]:
    """Run one poll cycle and return the resolved asset list.

    Nodes, and each configured custom property's populated values, are queried separately
    and then merged by NodeID, applying the three-source priority chain per node: a
    configured GPS custom property first, then Orion.Nodes.Location, then the City custom
    property geocoded through Nominatim. Each result's "source" names exactly which one won
    for that node.
    """
    now = datetime.now(timezone.utc).isoformat()

    nodes = swis.query(QUERY_NODES)

    custom_values: dict[str, dict[int, str]] = {}
    names_to_fetch = list(gps_property_names)
    if city_property_name and city_property_name not in names_to_fetch:
        names_to_fetch.append(city_property_name)
    for prop_name in names_to_fetch:
        rows = swis.query(build_custom_property_query(prop_name))
        custom_values[prop_name] = {row["NodeID"]: row["PropValue"] for row in rows}

    snapshot: list[dict[str, Any]] = []
    for row in nodes:
        node_id = row["NodeID"]
        caption = row.get("Caption") or f"Node {node_id}"
        status = row.get("StatusName") or "Unknown"

        lat_lon = None
        source = None

        for prop_name in gps_property_names:
            parsed = parse_lat_lon(custom_values.get(prop_name, {}).get(node_id))
            if parsed:
                lat_lon = parsed
                source = f"customProperty:{prop_name}"
                break

        if lat_lon is None:
            parsed = parse_lat_lon(row.get("Location"))
            if parsed:
                lat_lon = parsed
                source = "location"

        if lat_lon is None and city_property_name and geocoder is not None:
            city = custom_values.get(city_property_name, {}).get(node_id)
            if city:
                geocoded = geocoder.geocode(city)
                if geocoded:
                    lat_lon = geocoded
                    source = f"city:{city}"

        if lat_lon is None:
            continue

        snapshot.append(
            {
                "nodeId": node_id,
                "caption": caption,
                "lat": lat_lon[0],
                "lon": lat_lon[1],
                "source": source,
                "status": status,
                "lastPollUtc": now,
            }
        )

    return snapshot


def push_snapshot(session: requests.Session, push_url: str, push_token: str, snapshot: list[dict]) -> bool:
    """POST the snapshot outbound. Returns True on success.

    Retry/no-retry rules adapted from building-integrations.md section 7's retry-by-cause
    table, written there for calls into SWIS and adapted here for an outbound HTTP push:
    connection errors and 5xx are transient and retried with backoff; 401/403 mean the
    bearer token is wrong and retrying only repeats the same rejection, so those fail loud
    and the cycle's push is skipped rather than retried.
    """
    headers = {
        "Authorization": f"Bearer {push_token}",
        "Content-Type": "application/json",
    }
    delay = 1.0
    for attempt in range(1, 5):
        try:
            response = session.post(
                push_url, json={"assets": snapshot}, headers=headers, timeout=(5, 15)
            )
        except requests.RequestException as exc:
            if attempt == 4:
                LOG.error("push failed after %d attempts: %s", attempt, exc)
                return False
            LOG.warning("push attempt %d/4 failed (%s); retrying in %.1fs", attempt, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 15.0)
            continue

        if response.status_code == 200:
            LOG.info("pushed %d asset(s)", len(snapshot))
            return True
        if response.status_code in (401, 403):
            LOG.error(
                "push rejected with HTTP %d: PUSH_TOKEN is missing or wrong. Not retrying.",
                response.status_code,
            )
            return False
        if 500 <= response.status_code < 600:
            if attempt == 4:
                LOG.error("push failed after %d attempts: HTTP %d", attempt, response.status_code)
                return False
            LOG.warning(
                "push attempt %d/4 got HTTP %d; retrying in %.1fs",
                attempt, response.status_code, delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 15.0)
            continue
        LOG.error("push rejected with HTTP %d: %s. Not retrying.", response.status_code, response.text[:500])
        return False
    return False


_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    LOG.info("received signal %d; shutting down after this cycle", signum)
    _shutdown = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the resolved snapshot as JSON instead of pushing it, run once, and exit",
    )
    args = parser.parse_args()

    password = os.environ.get("SWIS_PASSWORD")
    if not password:
        sys.exit("SWIS_PASSWORD is not set. Do not pass the password on the command line.")

    host = os.environ.get("SWIS_HOST")
    user = os.environ.get("SWIS_USER")
    if not host or not user:
        sys.exit("SWIS_HOST and SWIS_USER must be set.")

    verify_tls = os.environ.get("SWIS_VERIFY_TLS", "true").strip().lower() not in ("false", "0", "no")
    ca_bundle = os.environ.get("SWIS_CA_BUNDLE") or None
    if not verify_tls:
        LOG.warning(
            "SWIS_VERIFY_TLS=false: TLS certificate verification is DISABLED for the SWIS "
            "connection. This connection carries the SWIS_PASSWORD credential. This is a "
            "real security tradeoff, not a cosmetic setting; see README.md 'TLS verification'."
        )

    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "20"))
    gps_candidates = [
        name.strip()
        for name in os.environ.get("SWIS_GPS_CUSTOM_PROPERTIES", "").split(",")
        if name.strip()
    ]
    city_property_candidate = os.environ.get("SWIS_CITY_CUSTOM_PROPERTY", "City").strip() or None

    push_url = os.environ.get("PUSH_URL")
    push_token = os.environ.get("PUSH_TOKEN")
    if not args.dry_run and (not push_url or not push_token):
        sys.exit("PUSH_URL and PUSH_TOKEN must be set unless --dry-run is used.")

    geocode_cache_path = Path(os.environ.get("GEOCODE_CACHE_PATH", DEFAULT_GEOCODE_CACHE_PATH))
    nominatim_user_agent = os.environ.get(
        "NOMINATIM_USER_AGENT", "swis-live-poller/1.0 (contact: strife1012@gmail.com)"
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    swis = Swis(host, user, password, verify=verify_tls, ca_bundle=ca_bundle)
    http_session = requests.Session()
    try:
        present = preflight(swis)
        known_props = discover_custom_properties(swis, present)

        gps_properties = [name for name in gps_candidates if name in known_props]
        for name in gps_candidates:
            if name not in known_props:
                LOG.warning("SWIS_GPS_CUSTOM_PROPERTIES entry %r not defined on this install; skipping", name)
        if gps_properties:
            LOG.info("polling GPS custom properties: %s", ", ".join(gps_properties))

        city_property = None
        geocoder = None
        if city_property_candidate:
            if city_property_candidate in known_props:
                city_property = city_property_candidate
                geocoder = Geocoder(geocode_cache_path, nominatim_user_agent)
                LOG.info("city fallback enabled via custom property %r", city_property)
            else:
                LOG.warning(
                    "city fallback property %r not defined on this install; city fallback disabled",
                    city_property_candidate,
                )

        while True:
            cycle_started = time.monotonic()
            try:
                snapshot = poll_snapshot(swis, gps_properties, city_property, geocoder)
            except SwisPermanentError as exc:
                LOG.error("permanent SWIS error, not retrying this cycle: %s", exc)
                snapshot = None
            except SwisError as exc:
                LOG.error("SWIS query failed for this cycle: %s", exc)
                snapshot = None

            if snapshot is not None:
                if args.dry_run:
                    print(json.dumps({"assets": snapshot}, indent=2))
                    return
                push_snapshot(http_session, push_url, push_token, snapshot)
            elif args.dry_run:
                return

            if _shutdown:
                break
            elapsed = time.monotonic() - cycle_started
            time.sleep(max(0.0, poll_interval - elapsed))
    finally:
        swis.close()
        http_session.close()


if __name__ == "__main__":
    main()

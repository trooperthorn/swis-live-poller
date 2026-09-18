#!/usr/bin/env python3
"""poller.py: poll SWIS for node GPS positions and push a snapshot outbound.

Reads Orion.Nodes.Location (free text, sometimes "lat,lon") and any configured
Orion.NodesCustomProperties columns, merges the matches, and POSTs a compact JSON array
to PUSH_URL for the OSH-A-ORG-2 Cloudflare Pages Function to store and re-serve.

Run once with --dry-run to see the snapshot printed instead of pushed. Otherwise it loops
forever at POLL_INTERVAL_SECONDS until it receives SIGINT/SIGTERM.

See README.md for every environment variable, the SWIS account rights this needs (read
only, no special rights), and how to re-validate the queries/*.swql files this script
issues against a real SolarWinds_OrionGuides checkout.
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

import requests

from swis_client import Swis, SwisError, SwisPermanentError

LOG = logging.getLogger("swis-live-poller")

# SolarWinds' Worldwide Map geocoder accepts "lat,lon" typed into Location. This is this
# script's best-effort reimplementation of that format: optional leading minus, digits,
# optional decimal portion, a comma, then the same shape for longitude, with no extra
# whitespace. It has not been checked against SolarWinds' own geocoder source, only against
# how populated Location values look in practice. Sanity-check it against a real populated
# node before relying on it; see README "Verify this yourself" section.
LAT_LON_RE = re.compile(
    r"^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$"
)

ENTITIES_NEEDED = ["Orion.Nodes", "Orion.StatusInfo"]
ENTITIES_OPTIONAL = ["Orion.NodesCustomProperties", "Orion.CustomProperty"]

QUERY_NODES_WITH_LOCATION = """
SELECT n.NodeID, n.Caption, n.Location, n.Status, s.StatusName
FROM Orion.Nodes n
JOIN Orion.StatusInfo s ON n.Status = s.StatusId
WHERE n.Location IS NOT NULL
ORDER BY n.NodeID
"""

QUERY_DISCOVER_CUSTOM_PROPERTIES = """
SELECT cp.Field, cp.DataType, cp.MaxLength
FROM Orion.CustomProperty cp
WHERE cp.Table = @table
ORDER BY cp.Field
"""


def build_custom_property_query(property_name: str) -> str:
    """Build a SELECT for one custom property's populated values.

    Property names cannot be bound as SWQL parameters; parameters are values, not
    identifiers (building-integrations.md section 5). So the name is formatted into the
    query text here, but only after discover_gps_custom_properties() has confirmed it
    against Orion.CustomProperty, which is schema data from the server itself and not
    caller-supplied text at this point.
    """
    return f"""
SELECT n.NodeID, n.Caption, n.Status, s.StatusName,
       n.CustomProperties.{property_name} AS GpsValue
FROM Orion.Nodes n
JOIN Orion.StatusInfo s ON n.Status = s.StatusId
WHERE n.CustomProperties.{property_name} IS NOT NULL
ORDER BY n.NodeID
"""


def preflight(swis: Swis) -> set[str]:
    """Confirm the entities this script depends on exist on this server.

    Feature-detect rather than version-check, per building-integrations.md section 9.
    Raises SystemExit if a required entity is absent; logs and continues if an optional
    one is (the custom-property path is simply skipped).
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


def discover_gps_custom_properties(swis: Swis, candidates: list[str], present: set[str]) -> list[str]:
    """Return the subset of SWIS_GPS_CUSTOM_PROPERTIES that actually exist on this server.

    Queries Orion.CustomProperty (Table = 'NodesCustomProperties') rather than trusting the
    configured names, so a stale or misspelled name in config is skipped with a warning
    instead of producing a runtime query error.
    """
    if not candidates:
        return []
    if "Orion.NodesCustomProperties" not in present or "Orion.CustomProperty" not in present:
        return []
    rows = swis.query(QUERY_DISCOVER_CUSTOM_PROPERTIES, table="NodesCustomProperties")
    actual = {row["Field"] for row in rows}
    found = [name for name in candidates if name in actual]
    for name in candidates:
        if name not in actual:
            LOG.warning("configured custom property %r not found on Orion.NodesCustomProperties; skipping", name)
    return found


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


def poll_snapshot(swis: Swis, gps_property_names: list[str]) -> list[dict]:
    """Run one poll cycle and return the merged asset list.

    Location and each configured custom property are queried separately and paged, then
    merged by NodeID. When a node matches more than one source, Location wins and the
    custom-property match is dropped, so the reported "source" always names exactly one
    place the coordinate came from. This is a documented convention, not something SWIS
    tells us to prefer; see README "Merge convention".
    """
    now = datetime.now(timezone.utc).isoformat()
    by_node: dict[int, dict] = {}

    for row in swis.paged(QUERY_NODES_WITH_LOCATION, page_size=500):
        coords = parse_lat_lon(row.get("Location"))
        if coords is None:
            continue
        lat, lon = coords
        by_node[row["NodeID"]] = {
            "nodeId": row["NodeID"],
            "caption": row.get("Caption"),
            "lat": lat,
            "lon": lon,
            "source": "location",
            "status": row.get("StatusName"),
            "lastPollUtc": now,
        }

    for prop_name in gps_property_names:
        query = build_custom_property_query(prop_name)
        for row in swis.paged(query, page_size=500):
            if row["NodeID"] in by_node:
                continue  # Location already claimed this node; see merge convention above.
            coords = parse_lat_lon(row.get("GpsValue"))
            if coords is None:
                continue
            lat, lon = coords
            by_node[row["NodeID"]] = {
                "nodeId": row["NodeID"],
                "caption": row.get("Caption"),
                "lat": lat,
                "lon": lon,
                "source": f"customProperty:{prop_name}",
                "status": row.get("StatusName"),
                "lastPollUtc": now,
            }

    return list(by_node.values())


def push_snapshot(session: requests.Session, push_url: str, push_token: str, snapshot: list[dict]) -> bool:
    """POST the snapshot outbound. Returns True on success.

    Retry/no-retry rules adapted from building-integrations.md section 7's retry-by-cause
    table, which is written for calls into SWIS. Adapted here for an outbound HTTP push:
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
            response = session.post(push_url, json=snapshot, headers=headers, timeout=(5, 15))
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
            LOG.error("push rejected with HTTP %d: PUSH_TOKEN is missing or wrong. Not retrying.",
                       response.status_code)
            return False
        if 500 <= response.status_code < 600:
            if attempt == 4:
                LOG.error("push failed after %d attempts: HTTP %d", attempt, response.status_code)
                return False
            LOG.warning("push attempt %d/4 got HTTP %d; retrying in %.1fs",
                        attempt, response.status_code, delay)
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
    parser.add_argument("--dry-run", action="store_true",
                         help="print the snapshot as JSON instead of POSTing it, run once, and exit")
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

    push_url = os.environ.get("PUSH_URL")
    push_token = os.environ.get("PUSH_TOKEN")
    if not args.dry_run and (not push_url or not push_token):
        sys.exit("PUSH_URL and PUSH_TOKEN must be set unless --dry-run is used.")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    swis = Swis(host, user, password, verify=verify_tls, ca_bundle=ca_bundle)
    http_session = requests.Session()
    try:
        present = preflight(swis)
        gps_properties = discover_gps_custom_properties(swis, gps_candidates, present)
        if gps_properties:
            LOG.info("polling custom properties: %s", ", ".join(gps_properties))

        while True:
            cycle_started = time.monotonic()
            try:
                snapshot = poll_snapshot(swis, gps_properties)
            except SwisPermanentError as exc:
                LOG.error("permanent SWIS error, not retrying this cycle: %s", exc)
                snapshot = None
            except SwisError as exc:
                LOG.error("SWIS query failed for this cycle: %s", exc)
                snapshot = None

            if snapshot is not None:
                if args.dry_run:
                    print(json.dumps(snapshot, indent=2))
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

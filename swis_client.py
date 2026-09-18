"""A minimal SWIS REST client, built directly on requests rather than the official orionsdk.

SolarWinds_OrionGuides docs/guides/building-integrations.md section 3 notes that the
official Python client's constructor overwrites a passed-in requests.Session's auth,
headers and verify, so a caller cannot use session= to install a pinned CA bundle. Building
directly on requests avoids that: SWIS_CA_BUNDLE is honored exactly as requested, and TLS
verification defaults to on (the opposite of the official client's own default) per section
3's guidance to verify TLS rather than accept the traditional self-signed certificate.

One client is created per process and reused for the life of the run (section 4): a single
requests.Session with a bound base URL, explicit connect and read timeouts set apart from
each other, and Basic auth. Every call here is POST /Query, which is the only interface this
poller needs; see README.md "What this does NOT do".
"""

from __future__ import annotations

from typing import Any

import requests

CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 30


class SwisError(Exception):
    """A SWIS query failed in a way that is worth retrying next cycle."""


class SwisPermanentError(SwisError):
    """A SWIS query failed in a way retrying will not fix (401/403/400)."""


class Swis:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 17774,
        verify: bool | str = True,
        ca_bundle: str | None = None,
    ) -> None:
        self._base_url = f"https://{host}:{port}/SolarWinds/InformationService/v3"
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers["Content-Type"] = "application/json"
        # ca_bundle, when given, takes precedence over the plain verify flag: it is the
        # "point the client at a pinned certificate" path building-integrations.md prefers.
        self._session.verify = ca_bundle if ca_bundle else verify

    def query(self, swql: str, **parameters: Any) -> list[dict[str, Any]]:
        """Run one SWQL query and return its rows. Always a POST /Query; read only."""
        body: dict[str, Any] = {"query": swql}
        if parameters:
            body["parameters"] = parameters
        try:
            response = self._session.post(
                f"{self._base_url}/Query",
                json=body,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise SwisError(f"query failed: {exc}") from exc

        if response.status_code in (400, 401, 403):
            raise SwisPermanentError(
                f"query rejected with HTTP {response.status_code}: {response.text[:500]}"
            )
        if response.status_code >= 500:
            raise SwisError(f"query failed with HTTP {response.status_code}")
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SwisError(f"query failed: {exc}") from exc

        return payload.get("results", [])

    def close(self) -> None:
        self._session.close()

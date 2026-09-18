# swis-live-poller

Polls a SolarWinds Observability Self-Hosted (SWIS/Orion) server for node positions and
pushes a compact JSON snapshot to a Cloudflare Pages Function, so a public PWA (OSH-A-ORG-2)
can plot live vehicle/train positions without ever talking to SWIS directly. SWIS is LAN
only and not internet reachable; this poller is the only thing that bridges the two.

## What it does

Every `POLL_INTERVAL_SECONDS` (default 20):

1. Queries every configured custom property in `SWIS_GPS_CUSTOM_PROPERTIES` (joined through
   `Orion.NodesCustomProperties`), after confirming each name actually exists via
   `Orion.CustomProperty` (`Table = 'NodesCustomProperties'`), for a value matching `lat,lon`.
2. Falls back to `Orion.Nodes.Location` for any node not resolved in step 1, if it matches
   the same `lat,lon` shape.
3. Falls back to the `City` custom property (if present on this install) for any node still
   unresolved, geocoded through Nominatim.
4. Resolves each node with the first source that produces a usable coordinate, in that order
   (`resolve_positions` in `poller.py`), and reports which one won as `source`:
   `"customProperty:<name>"`, `"location"`, or `"city:<name>"`. A GPS custom property beats
   `Location`, and both beat `City`, because a custom property was set up specifically to
   carry this asset's coordinates, `Location` is a general-purpose free-text field that
   happens to sometimes hold coordinates, and `City` is a place name requiring a geocode and
   only ever yields a city centroid, not the asset's real position.
5. POSTs the resolved list to `PUSH_URL` with `Authorization: Bearer <PUSH_TOKEN>`.

Nothing is written back to SWIS. Nothing is written to disk between polls; the snapshot
lives only in memory for the duration of one push.

## What this does NOT do

- No writes to SWIS. Every query this script issues is a read (`POST /Query`); there is no
  Invoke or CRUD call anywhere in this codebase.
- No persistence beyond the current in-memory snapshot. If the process restarts, the last
  snapshot is gone; the next poll cycle produces a fresh one. There is no local database,
  cache file, or queue.
- No auth flow beyond the bearer token in `PUSH_TOKEN`. There is no OAuth, no session, no
  refresh logic. Rotate the token by changing it on both ends.
- No historical query or time bounding. This is a live poll, not a report; SWIS's own
  historical/statistics tables are never touched.

## SWIS account requirements

Create a dedicated account for this poller (do not reuse another integration's account or a
person's login; see `building-integrations.md` section 2 in SolarWinds_OrionGuides). It
needs **read-only, default rights only**:

- No `allowUnmanage`, no `manageNodes`, no `admin` right. This poller never calls a verb.
- Grant it read access to `Orion.Nodes`, `Orion.StatusInfo`, `Orion.NodesCustomProperties`,
  and `Orion.CustomProperty`. All four are read at the default `everyone` access level in
  the schema this repo validated against (2026.2); no elevated right is required.
- An account limitation is optional. If you scope this account to a subset of nodes, only
  nodes inside that scope will ever appear in the pushed snapshot, and SWIS applies the
  limitation silently (no error, just fewer rows). That is a feature here, not a bug: it is
  how you'd restrict which assets get published to a public PWA.

## Environment variables

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `SWIS_HOST` | yes | | SWIS server hostname or IP |
| `SWIS_USER` | yes | | SWIS account username |
| `SWIS_PASSWORD` | yes | | SWIS account password. Environment only, never a CLI argument, never in source |
| `SWIS_VERIFY_TLS` | no | `true` | Set `false` only as a deliberate, stated tradeoff (see below) |
| `SWIS_CA_BUNDLE` | no | | Path to a PEM bundle if SWIS presents a certificate from an internal CA not already in the system trust store |
| `POLL_INTERVAL_SECONDS` | no | `20` | Seconds between poll cycles |
| `SWIS_GPS_CUSTOM_PROPERTIES` | no | empty | Comma-separated candidate custom property names to check, e.g. `GPSLocation,VehiclePosition`. Each is verified against `Orion.CustomProperty` at startup; unknown names are logged and skipped, not guessed at |
| `SWIS_CITY_CUSTOM_PROPERTY` | no | `City` | Name of the last-resort, place-name custom property to geocode. Set to empty to disable the City/Nominatim fallback entirely. Verified against `Orion.CustomProperty` the same way as the GPS candidates |
| `PUSH_URL` | yes (unless `--dry-run`) | | Full URL of the Cloudflare Pages Function ingest endpoint |
| `PUSH_TOKEN` | yes (unless `--dry-run`) | | Bearer token the ingest endpoint expects in `Authorization: Bearer <token>` |
| `GEOCODE_CACHE_PATH` | no | `geocode_cache.json` | Local file the City→coordinates cache is persisted to, so a restart does not re-geocode every city |
| `NOMINATIM_USER_AGENT` | no | `swis-live-poller/1.0 (contact: strife1012@gmail.com)` | Sent on every Nominatim request; Nominatim's usage policy requires a descriptive User-Agent with contact information |

See `.env.example` for a template. Copy it to `.env` (gitignored) for local use, or set
these directly in whatever process manager runs the poller.

## TLS verification

The script defaults to `SWIS_VERIFY_TLS=true`, which is the opposite of the official SWIS
Python client's own default (`verify=False`). If you have not issued SWIS a certificate
from an internal CA, `SWIS_VERIFY_TLS=true` with no `SWIS_CA_BUNDLE` will fail against
SWIS's default self-signed certificate, because the system trust store will not recognize
it.

You have two real options, and only one of them is a deliberate tradeoff rather than a gap:

- **Preferred:** issue SWIS a certificate from an internal CA (or export its self-signed
  certificate once) and point `SWIS_CA_BUNDLE` at the PEM file. This is the option
  `building-integrations.md` recommends, and it is what this script is built to use.
- **Documented fallback:** set `SWIS_VERIFY_TLS=false`. This disables certificate
  verification on the connection carrying `SWIS_PASSWORD` to SWIS. Anyone positioned to
  intercept that connection (a compromised switch, a rogue DHCP/ARP actor on the same LAN
  segment) can read or replace SWIS's responses without your knowledge. This poller logs a
  loud warning every time it starts with this setting on, and this paragraph is that
  tradeoff stated plainly rather than swept under a default. Decide it deliberately; do not
  leave it off by accident.

## The lat,lon format

SolarWinds' own Worldwide Map geocoder accepts coordinates typed into `Location` as
`lat,lon`. This poller's regex (`poller.py`, `LAT_LON_RE`) is this project's best-effort
reimplementation of that format: optional leading minus, digits, optional decimal portion,
a comma, the same shape for longitude, no extra whitespace. It has **not** been checked
against SolarWinds' own geocoder source or documentation beyond what's referenced in
SolarWinds_OrionGuides. Before relying on this in production, populate a real test node's
`Location` field with a known coordinate through the Orion web console and confirm the
poller parses it as expected with `--dry-run`.

## City fallback and Nominatim

`City` is documented in this repository's companion project, `SolarWinds_OrionGuides`, only
as an example custom property name that ships by convention on SolarWinds installs (see
`docs/schema/key-entities.md` there). The extracted 2026.2 schema shows `Orion.NodesCustomProperties`
carrying exactly one property, `NodeID` -- everything else, `City` included, is per-install
data, not a schema fact this repository can verify. **Whether `City` is genuinely present on
every SolarWinds installation is unverified; confirm it exists on your own server (the
poller does this automatically via `Orion.CustomProperty` and logs a warning and disables
the fallback if it is absent) before relying on it.**

When a node has no usable GPS custom property and no usable `Location`, the poller geocodes
its `City` value (default property name, overridable via `SWIS_CITY_CUSTOM_PROPERTY`)
through OpenStreetMap's Nominatim search API. This is a deliberate last resort:

- The result is a city centroid, not the asset's real position. A vehicle in city traffic
  and a vehicle at the edge of town both geocode to the same point. The pushed snapshot's
  `source` field is `"city:<name>"` for these, so the PWA can render them with a visibly
  coarser/approximate treatment instead of implying precision the data does not have.
- Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/) caps
  free use at one request per second, requires a descriptive `User-Agent` naming the tool and
  a contact, and asks callers to cache results rather than re-querying. This poller enforces
  the rate limit in `Geocoder.geocode()`, sends `NOMINATIM_USER_AGENT`, and caches every
  lookup (success or miss) both in memory and to `GEOCODE_CACHE_PATH` on disk, keyed on the
  exact city string, so a restart does not re-geocode cities it has already resolved.

## Custom property discovery

`SWIS_GPS_CUSTOM_PROPERTIES` is a list of *candidate* names, not an assumed contract. At
startup the poller queries `Orion.CustomProperty` (`WHERE Table = 'NodesCustomProperties'`)
to see what actually exists on the target server, and only polls the names that are
confirmed present. This means the same poller binary works across different SWIS
installations that name their GPS custom property differently, without a code change; only
the environment variable changes per deployment.

## Validating the shipped queries

Every `.swql` file under `queries/` is written to be checked with
`SolarWinds_OrionGuides/tools/validate_swql.py`. To re-validate them yourself:

```bash
cd /path/to/SolarWinds_OrionGuides
python tools/validate_swql.py /path/to/swis-live-poller/queries/*.swql
```

`queries/nodes_with_custom_property_value.example.swql` is the one exception: it is a
worked example of the query shape the poller builds at runtime
(`poller.py:build_custom_property_query`) using a placeholder property name
(`ExampleGpsField`) that does not exist on any real server, so the validator correctly
reports it as an unknown property. That failure is expected and is explained in a comment
at the top of the file; it demonstrates the *shape*, not a literal query this repo ships.
The other four files validate clean as of this repo's initial commit; the validator output
from that run is included in the commit history for reference.

## Retry behavior

Adapted from `building-integrations.md` section 7's retry-by-cause table, which is written
for calls *into* SWIS. This poller adapts the same reasoning to its one outbound HTTP push
(see the comment above `push_snapshot` in `poller.py` for the adaptation, spelled out):

| Outcome | Retried? |
| --- | --- |
| Connection error / timeout on the SWIS query | Yes, with backoff (queries are read-only, always safe to repeat) |
| SWIS returns 401/403 | No; `SwisPermanentError`, fails the cycle loudly |
| Connection error / timeout on the outbound push | Yes, with backoff |
| Push returns 5xx | Yes, with backoff |
| Push returns 401/403 | No; the bearer token is wrong, logged as an error, cycle's push is skipped |
| Push returns any other 4xx | No; logged as an error, cycle's push is skipped |

All retries cap at 4 attempts with exponential backoff plus jitter. Nothing retries forever.

## Running it

```bash
python -m venv .venv
.venv/Scripts/activate    # or source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
cp .env.example .env      # edit with real values; .env is gitignored

# One-off, prints the snapshot instead of pushing it:
python poller.py --dry-run

# Long-running:
python poller.py
```

`poller.py` runs as a plain `while True` loop with `SIGINT`/`SIGTERM` handled for clean
shutdown after the in-flight cycle finishes. It is meant to be supervised by systemd, NSSM,
or Windows Task Scheduler, not to daemonize itself.

### Linux (systemd)

See `deploy/swis-live-poller.service`. It reads secrets from an `EnvironmentFile`, not from
the unit file itself.

### Windows

Sean's LAN is mixed OS, so pick whichever fits the host this ends up running on:

- **NSSM** (recommended if already used elsewhere on the LAN): `nssm install
  swis-live-poller "C:\path\to\.venv\Scripts\python.exe" "C:\path\to\poller.py"`, then set
  the environment variables under the service's Environment tab (or via `nssm set
  swis-live-poller AppEnvironmentExtra ...`), and set Startup type to Automatic.
- **Windows Task Scheduler**: create a task triggered "At startup", action running
  `C:\path\to\.venv\Scripts\python.exe C:\path\to\poller.py`, with "Run whether user is
  logged on or not". Task Scheduler does not read `.env` files; set the environment
  variables on the account that runs the task, or wrap the action in a small `.cmd` that
  sets them before calling `poller.py`. A scheduled task is not automatically restarted if
  the process exits; if the host doesn't already have a supervisor pattern for this,
  NSSM's Restart behavior is the simpler choice.

Neither of these has been exercised for this repo; treat both as starting points to adapt to
whatever host actually runs this.

## No secrets in this repo

`.gitignore` covers `.env` and `__pycache__/`. `.env.example` contains only placeholder
values. Before any commit, run `git status` and read every new/changed file; do not stage
anything you have not read.

"""
Live end-to-end test for the airspace_watch modular input.

The real data source is a tar1090/dump1090 receiver on the home LAN (default
192.168.0.58:8080), which no CI runner can reach. A live test pointed at it
would only ever SKIP, proving nothing. So instead we stand up a mock tar1090
receiver inside the compose network (docker/docker-compose.yml -> mock-receiver)
serving a canned aircraft.json (tests/fixtures/data/aircraft.json), point a
short-interval input at it, and assert what the add-on actually has to do:

  * events land in index=main sourcetype=airspace:adsb
  * Splunk extracted the JSON fields the dashboard/downstream searches rely on
  * the haversine geofilter kept the in-radius aircraft and dropped the far one
  * the input logged none of its own fetch/parse/import errors

Mocking the receiver makes this layer deterministic and network-free, so it also
runs on runners with no egress. On the `live` backend (no mock-receiver on the
LAN) the whole module self-skips rather than failing misleadingly.
"""
from __future__ import annotations

import time

import pytest

APP = "TA-airspace-watch"
STANZA = "ci_probe"
INDEX = "main"
SOURCETYPE = "airspace:adsb"

# Must match the mock service hostname in docker-compose.yml and the canned
# fixture. Centre = Leeds, 50 nm radius.
RECEIVER_HOST = "mock-receiver"
RECEIVER_PORT = "8080"
RECEIVER_PATH = "/data/aircraft.json"
CENTER_LAT = "53.8"
CENTER_LON = "-1.55"
RADIUS_NM = "50"

# Aircraft the fixture places inside the radius, and the one it puts well outside
# (Madrid) that the geofilter must drop. Hex values are lower-cased by the script.
INSIDE_HEXES = {"4008f2", "407a3c", "4ca7b5"}
OUTSIDE_HEX = "cafef0"

# `search` app context is writable by the container's splunk user without
# touching the bind-mounted app dir; the app namespace is a fallback.
CREATE_PATHS = (
    "/servicesNS/nobody/search/data/inputs/airspace_watch",
    f"/servicesNS/nobody/{APP}/data/inputs/airspace_watch",
)

POLL_SECONDS = 180
POLL_INTERVAL = 10


def _mock_reachable(splunk) -> bool:
    """True only when the mock receiver resolves from inside Splunk (docker backend)."""
    import shutil

    from conftest import docker_exec

    if not shutil.which("docker"):
        return False
    # curl ships in the splunk/splunk image (its own healthcheck uses it).
    rc, _out, _err = docker_exec(
        "curl", "-fsS", f"http://{RECEIVER_HOST}:{RECEIVER_PORT}{RECEIVER_PATH}",
        timeout=30,
    )
    return rc == 0


@pytest.fixture(scope="module")
def airspace_input(splunk):
    """Create a short-interval input pointed at the mock receiver; remove on teardown."""
    if not _mock_reachable(splunk):
        pytest.skip(
            f"mock receiver {RECEIVER_HOST}:{RECEIVER_PORT} not reachable from Splunk "
            "(this live test needs the docker backend with the mock-receiver service)"
        )

    params = {
        "name": STANZA,
        "index": INDEX,
        "sourcetype": SOURCETYPE,
        "interval": "10",
        "adsb_host": RECEIVER_HOST,
        "adsb_port": RECEIVER_PORT,
        "adsb_path": RECEIVER_PATH,
        "center_lat": CENTER_LAT,
        "center_lon": CENTER_LON,
        "radius_nm": RADIUS_NM,
        "request_timeout": "4",
    }
    # NB: this modular-input create handler rejects a "disabled" arg on POST
    # ("Argument \"disabled\" is not supported by this handler"). We enable the
    # stanza explicitly below (line ~104) rather than passing disabled=0 here.
    used, last = None, ""
    for path in CREATE_PATHS:
        status, body = splunk.request("POST", path, data=params)
        if status in (200, 201, 409):  # created, or already exists
            used = path
            break
        last = f"{status}: {body[:300]}"
    assert used, f"could not create airspace_watch input (last {last})"
    # Ensure it is enabled even if it pre-existed (409).
    splunk.request("POST", f"{used}/{STANZA}/enable")

    yield used

    splunk.request("DELETE", f"{used}/{STANZA}", params={"output_mode": "json"})


def _input_error_logged(splunk):
    spl = (
        "search index=_internal (source=*ta_airspace_watch.log* OR source=*splunkd.log*) "
        '"airspace_watch" ("fetch failed" OR "invalid configuration" '
        'OR "ImportError" OR "ModuleNotFoundError" OR "Traceback") '
        "earliest=-15m"
    )
    return splunk.search(spl, earliest="-15m")


def _collect(splunk):
    deadline = time.time() + POLL_SECONDS
    hits = []
    while time.time() < deadline:
        hits = splunk.search(
            f"search index={INDEX} sourcetype={SOURCETYPE} | head 50",
            earliest="-15m",
        )
        if hits:
            break
        time.sleep(POLL_INTERVAL)
    return hits


def test_airspace_events_indexed(splunk, airspace_input):
    hits = _collect(splunk)
    if not hits:
        errs = _input_error_logged(splunk)
        detail = [h.get("_raw", "")[:200] for h in errs[:3]] if errs else "none"
        pytest.fail(
            f"no {SOURCETYPE} events indexed within {POLL_SECONDS}s. "
            f"input error signatures in _internal: {detail}"
        )

    # Splunk extracted the JSON payload the script emits (props: KV_MODE=json).
    row = hits[0]
    missing = [
        f for f in ("hex", "lat", "lon", "altitude_ft", "distance_nm")
        if row.get(f) in (None, "")
    ]
    assert not missing, f"indexed event missing expected fields {missing}: {sorted(row)}"


def test_geofilter_keeps_inside_drops_outside(splunk, airspace_input):
    hits = _collect(splunk)
    assert hits, "no events to assert the geofilter against (see test_airspace_events_indexed)"

    seen_hexes = {h.get("hex") for h in hits}
    # The far (Madrid) aircraft must never be emitted.
    assert OUTSIDE_HEX not in seen_hexes, (
        f"geofilter leaked an out-of-radius aircraft {OUTSIDE_HEX}: {sorted(seen_hexes)}"
    )
    # At least one known in-radius aircraft made it through.
    assert seen_hexes & INSIDE_HEXES, (
        f"none of the in-radius aircraft {sorted(INSIDE_HEXES)} indexed; got {sorted(seen_hexes)}"
    )
    # And every emitted event is within the configured radius.
    over = [
        (h.get("hex"), h.get("distance_nm"))
        for h in hits
        if h.get("distance_nm") not in (None, "") and float(h["distance_nm"]) > float(RADIUS_NM)
    ]
    assert not over, f"events emitted beyond radius {RADIUS_NM}nm: {over}"


def test_input_logged_no_error(splunk, airspace_input):
    # The fetch to the mock receiver should succeed; assert the input's own error
    # signatures are absent (a fetch/parse/import failure would show here).
    _collect(splunk)  # ensure at least one poll cycle has run
    errs = _input_error_logged(splunk)
    assert not errs, f"airspace_watch logged errors: {[h.get('_raw', '')[:200] for h in errs[:3]]}"

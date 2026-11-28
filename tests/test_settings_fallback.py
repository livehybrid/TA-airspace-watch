"""The file fallback for the Configuration settings honours Splunk's precedence.

When the collector cannot read its settings through splunkd (no session key, or
conf_manager raising), it reads ta_airspace_watch_settings.conf from disk. local/
must override default/, as it does in splunkd. Until Nov 2026 the reader parsed
local/ first and then default/ into the same dict, so every default overwrote the
value saved on the Configuration page.

Runs the packaged collector module under Splunk's own Python in the container,
against a throwaway SPLUNK_HOME written for this test only.
"""
from __future__ import annotations

import json
import shutil

import pytest

from conftest import CONTAINER, docker_exec

APP = "TA-airspace-watch"
HOME = "/tmp/aios-settings-fallback"

DEFAULT = "[receiver]\nadsb_host = 192.168.0.58\nadsb_port = 8080\n\n[geofence]\nradius_nm = 50\n"
LOCAL = "[receiver]\nadsb_host = saved-host\n\n[logging]\nloglevel = DEBUG\n"

PROBE = f"""
import json, os, sys
conf_dir = {HOME!r} + "/etc/apps/{APP}"
for leaf, body in (("default", {DEFAULT!r}), ("local", {LOCAL!r})):
    os.makedirs(conf_dir + "/" + leaf, exist_ok=True)
    with open(conf_dir + "/" + leaf + "/ta_airspace_watch_settings.conf", "w") as fh:
        fh.write(body)
os.environ["SPLUNK_HOME"] = {HOME!r}
sys.path.insert(0, "/opt/splunk/etc/apps/{APP}/bin")
import airspace_watch
print("RESULT " + json.dumps(airspace_watch._settings_from_conf_files(), sort_keys=True))
"""


def _container_reachable():
    return bool(shutil.which("docker")) and docker_exec("true", timeout=15)[0] == 0


pytestmark = pytest.mark.skipif(not _container_reachable(),
                                reason=f"container {CONTAINER!r} not reachable (non-docker backend)")


def test_local_overrides_default_and_both_are_read():
    try:
        rc, out, err = docker_exec("/opt/splunk/bin/splunk", "cmd", "python3.13", "-c", PROBE)
        assert rc == 0, f"probe exited {rc}\n{out}\n{err}"
        line = [l for l in out.splitlines() if l.startswith("RESULT ")]
        assert line, f"no RESULT line\n{out}\n{err}"
        got = json.loads(line[-1][len("RESULT "):])
    finally:
        docker_exec("rm", "-r", HOME)
    # Saved value wins, unsaved default survives, stanzas only in either file are kept.
    assert got["receiver"] == {"adsb_host": "saved-host", "adsb_port": "8080"}, got
    assert got["geofence"] == {"radius_nm": "50"}, got
    assert got["logging"] == {"loglevel": "DEBUG"}, got

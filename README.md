# TA-airspace-watch

Splunk modular input that polls a local tar1090 / dump1090-fa receiver and
emits one Splunk event per aircraft on every poll cycle. It is the
production data source for the **AirspaceWatch** dashboard, our entry in
the Splunk Dashboard Contest 2026.

- **Receiver default:** `http://192.168.0.58:8080/data/aircraft.json`
- **Default geofence:** 50 nautical miles around Leeds (53.8 N, -1.55 E)
- **Default sourcetype:** `airspace:adsb` (see `package/default/props.conf`)
- **Default poll interval:** 5 seconds

## Compatibility

| Attribute | Value |
|-----------|-------|
| **Add-on version** | 1.0.0 |
| **Tested against** | Splunk Enterprise 10.0, Python 3.9 (real Splunk in Docker, on every CI run) |
| **Python runtime** | 3.9, Splunk's long-term-support runtime |
| **Expected compatible** | Splunk Enterprise and Cloud 9.3+ and 10.x (any release on the Python 3.9 runtime) |
| **Deployment roles** | Standalone, Distributed, Search Head Clustering |
| **AppInspect** | Passes the `cloud`, `future` and `private_victoria` tag sets |

Splunk 9.3 through 10.1 default to Python 3.9, and 3.9 stays the LTS runtime on
10.2 and later, so an add-on that is clean on 3.9 runs unchanged across that
whole range. This add-on is validated on 3.9 and pins its vendored libraries to
versions that stay 3.9-clean. It is not yet validated on the opt-in Python 3.13
runtime introduced in Splunk 10.2.

## Repository layout

```
TA-airspace-watch/
├── globalConfig.json                  UCC UI definition (Configuration + Inputs tabs)
├── requirements.txt                   Top-level build/test deps
├── README.md
└── package/
    ├── app.manifest                   App identity + version
    ├── app.conf                       Mirror of manifest for splunkd
    ├── bin/
    │   ├── import_declare_test.py     sys.path bootstrap
    │   └── airspace_watch.py          Modular input (Splunk SDK Script subclass)
    ├── default/
    │   ├── app.conf                   Default app config
    │   ├── inputs.conf                Default input stanza
    │   ├── props.conf                 Sourcetype definition for airspace:adsb
    │   └── server.conf                Conf-replication includes for SHC
    ├── lib/
    │   └── requirements.txt           Vendored runtime deps (ucc-gen --pip)
    └── README/
        ├── inputs.conf.spec
        └── airspace_watch_settings.conf.spec
```

## Build

The add-on is built with the Splunk UCC framework. ucc-gen reads
`globalConfig.json` + `package/`, vendors the Python deps in
`package/lib/requirements.txt` into `output/TA-airspace-watch/lib/`, and
generates the REST handlers and UI assets.

```bash
# One-off, from the repo root
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Build
ucc-gen build --source package --ta-version 0.1.0

# Package into a .spl tarball ready for SplunkBase
ucc-gen package --path output/TA-airspace-watch
```

The result lands in `output/TA-airspace-watch-*.spl`.

## App Inspect (required before SplunkBase upload)

```bash
splunk-appinspect inspect output/TA-airspace-watch-0.1.0.spl \
  --mode test \
  --included-tags cloud --included-tags splunk_appinspect \
  --output-file appinspect-report.html
```

Fix every failure. Warnings on the `cloud` tag (binaries in `lib/`, Python
version pins, etc.) must also be addressed before a Cloud-vetted release.

## Install locally for development

```bash
# After ucc-gen build
ln -s "$(pwd)/output/TA-airspace-watch" "$SPLUNK_HOME/etc/apps/TA-airspace-watch"
$SPLUNK_HOME/bin/splunk restart
```

Then in Splunk Web go to **Apps → AirspaceWatch → Configuration** to set
the receiver host/port and geofence, and **Apps → AirspaceWatch → Inputs**
to enable a polling stanza.

## Verify

After enabling the input, run:

```spl
index=main sourcetype=airspace:adsb earliest=-1m
| stats count by callsign hex
| sort - count
```

You should see one row per aircraft heard in the last minute. Each event
contains: `callsign`, `hex`, `lat`, `lon`, `altitude_ft`, `speed_kts`,
`heading`, `vertical_rate_fpm`, `distance_nm`, `squawk`, `aircraft_type`,
`category`, `rssi`, `last_seen`.

## SplunkBase submission

1. `ucc-gen build` then `ucc-gen package` — keep the `.spl` exactly as
   produced.
2. Run App Inspect with the `cloud` tag and attach the HTML report to
   the SplunkBase submission.
3. Tag the repo to match the manifest version (`git tag v0.1.0`).
4. Upload to <https://splunkbase.splunk.com/>.

## License

Apache 2.0 — see `LICENSES/LICENSE.txt`.

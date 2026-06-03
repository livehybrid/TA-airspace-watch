TA-airspace-watch — Release Notes

Version 0.1.0 (initial release)
-------------------------------
- Modular input ``airspace_watch`` polls a tar1090 / dump1090-fa receiver
  on a configurable interval (default 5s).
- Filters aircraft within a configurable radius (default 50 nm) of a
  configurable centre point (default Leeds: 53.8, -1.55) using the
  haversine formula.
- Emits one JSON event per aircraft, sourcetype ``airspace:adsb``.
- UCC configuration UI exposes Receiver, Geofence, and Logging tabs.
- Pure-Python, no bundled binaries. Splunk Cloud compatible.

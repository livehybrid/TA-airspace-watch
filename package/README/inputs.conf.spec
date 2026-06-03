[airspace_watch://<name>]
* Modular input that polls a local ADS-B receiver (tar1090 / dump1090-fa
*   JSON API) and emits one Splunk event per aircraft within the configured
*   radius of the configured centre point.
* interval, index, sourcetype are handled by Splunk natively — do not
*   redeclare them here as that triggers "internal argument" startup errors.

adsb_host = <string>
* Hostname or IP address of the ADS-B receiver running tar1090/dump1090.
* Default: 192.168.0.58

adsb_port = <integer>
* TCP port for the ADS-B receiver HTTP API.
* Default: 8080

adsb_path = <string>
* HTTP path for the aircraft JSON endpoint (tar1090 default is
*   /data/aircraft.json, dump1090-fa is /dump1090-fa/data/aircraft.json).
* Default: /data/aircraft.json

center_lat = <decimal>
* Centre latitude (degrees, WGS84) for the radius filter.
* Default: 53.8

center_lon = <decimal>
* Centre longitude (degrees, WGS84) for the radius filter.
* Default: -1.55

radius_nm = <decimal>
* Radius (nautical miles) around (center_lat, center_lon). Aircraft
*   outside the radius are not emitted.
* Default: 50

request_timeout = <integer>
* HTTP request timeout for each poll, in seconds.
* Default: 4

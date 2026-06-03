#!/usr/bin/env python3
"""
airspace_watch.py — Splunk modular input for ADS-B aircraft observations.

Polls a tar1090 / dump1090-fa receiver (e.g. http://192.168.0.58:8080/data/aircraft.json)
on the configured interval, filters aircraft within ``radius_nm`` of
``(center_lat, center_lon)`` using the haversine formula, and emits one
Splunk event per aircraft per poll cycle.

Sourcetype: airspace:adsb (see default/props.conf).

This is the production data source for the AirspaceWatch dashboard
(Splunk Dashboard Contest 2026).
"""

# Path bootstrap — must be first.
import import_declare_test  # noqa: F401

import json
import logging
import logging.handlers
import math
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from splunklib.modularinput import Argument, Event, EventWriter, Scheme, Script

try:
    # Provided by the UCC framework, vendored into package/lib at build time.
    from solnlib import conf_manager, log
    _HAS_SOLNLIB = True
except Exception:  # pragma: no cover - solnlib only present in a built TA
    _HAS_SOLNLIB = False

try:
    import requests
except ImportError:  # pragma: no cover - requests is bundled at build time
    requests = None  # type: ignore[assignment]


APP_NAME = "TA-airspace-watch"
SETTINGS_CONF = "airspace_watch_settings"
EARTH_RADIUS_NM = 3440.065  # nautical miles


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_file() -> str:
    splunk_home = os.environ.get("SPLUNK_HOME", "/opt/splunk")
    return os.path.join(splunk_home, "var", "log", "splunk", "ta_airspace_watch.log")


def _build_logger(level: str = "INFO") -> logging.Logger:
    """Rotating file logger that lives next to the other splunkd logs."""
    logger = logging.getLogger(APP_NAME)
    if logger.handlers:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        return logger

    handler = logging.handlers.RotatingFileHandler(
        _log_file(), maxBytes=25 * 1024 * 1024, backupCount=5
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles between two lat/lon pairs."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_NM * c


# ---------------------------------------------------------------------------
# ADS-B fetch + normalise
# ---------------------------------------------------------------------------

def fetch_aircraft_json(host: str, port: int, path: str, timeout: float) -> Dict[str, Any]:
    """Fetch the receiver's aircraft.json. Raises on any network or HTTP error."""
    if requests is None:
        raise RuntimeError(
            "The 'requests' library is not available. ucc-gen should vendor it "
            "into package/lib at build time."
        )
    url = "http://{host}:{port}{path}".format(host=host, port=port, path=path)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalise_aircraft(
    ac: Dict[str, Any],
    poll_time: float,
    center_lat: float,
    center_lon: float,
    radius_nm: float,
) -> Optional[Dict[str, Any]]:
    """
    Convert one tar1090/dump1090 aircraft entry into a normalised dict, or
    return None if the aircraft has no usable position fix or is outside the
    configured radius.
    """
    lat = _coerce_float(ac.get("lat"))
    lon = _coerce_float(ac.get("lon"))
    if lat is None or lon is None:
        return None

    distance = haversine_nm(center_lat, center_lon, lat, lon)
    if distance > radius_nm:
        return None

    # tar1090 fields:
    #   hex, flight (callsign), alt_baro / alt_geom, gs (ground speed),
    #   track / true_heading, squawk, t (ICAO type), seen, rssi, category, ...
    callsign = (ac.get("flight") or "").strip() or None
    altitude = ac.get("alt_baro")
    if altitude in ("ground", None):
        altitude = ac.get("alt_geom") if altitude in ("ground", None) else altitude
    heading = ac.get("track")
    if heading is None:
        heading = ac.get("true_heading")

    # tar1090 emits "seen" in seconds since the receiver last heard the aircraft.
    seen_seconds = _coerce_float(ac.get("seen"))
    last_seen_epoch = poll_time - seen_seconds if seen_seconds is not None else poll_time
    last_seen_iso = (
        datetime.fromtimestamp(last_seen_epoch, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )

    event_time_iso = (
        datetime.fromtimestamp(poll_time, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )

    return {
        "time": event_time_iso,
        "last_seen": last_seen_iso,
        "hex": (ac.get("hex") or "").lower() or None,
        "callsign": callsign,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "altitude_ft": _coerce_int(altitude),
        "speed_kts": _coerce_int(ac.get("gs")),
        "heading": _coerce_int(heading) if heading is not None else None,
        "vertical_rate_fpm": _coerce_int(ac.get("baro_rate") or ac.get("geom_rate")),
        "squawk": ac.get("squawk"),
        "aircraft_type": ac.get("t"),
        "category": ac.get("category"),
        "rssi": _coerce_float(ac.get("rssi")),
        "distance_nm": round(distance, 2),
        "receiver_seen_s": seen_seconds,
    }


# ---------------------------------------------------------------------------
# Settings loader (uses solnlib conf_manager when available)
# ---------------------------------------------------------------------------

def _settings_from_conf_manager(session_key: str) -> Dict[str, Dict[str, Any]]:
    """Read the configuration tab values via the UCC conf_manager helper."""
    cfm = conf_manager.ConfManager(
        session_key,
        APP_NAME,
        realm="__REST_CREDENTIAL__#{}#configs/conf-{}".format(APP_NAME, SETTINGS_CONF),
    )
    conf = cfm.get_conf(SETTINGS_CONF)
    return {stanza: conf.get(stanza) for stanza in conf.get_all().keys()}


def _settings_from_conf_files() -> Dict[str, Dict[str, Any]]:
    """Fallback: read airspace_watch_settings.conf directly."""
    splunk_home = os.environ.get("SPLUNK_HOME", "/opt/splunk")
    candidates = [
        os.path.join(splunk_home, "etc", "apps", APP_NAME, "local", SETTINGS_CONF + ".conf"),
        os.path.join(splunk_home, "etc", "apps", APP_NAME, "default", SETTINGS_CONF + ".conf"),
    ]
    settings: Dict[str, Dict[str, Any]] = {}
    for path in candidates:
        if not os.path.isfile(path):
            continue
        current = None
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    current = line[1:-1]
                    settings.setdefault(current, {})
                elif "=" in line and current is not None:
                    k, _, v = line.partition("=")
                    settings[current][k.strip()] = v.strip()
    return settings


def load_settings(session_key: Optional[str]) -> Dict[str, Dict[str, Any]]:
    if session_key and _HAS_SOLNLIB:
        try:
            return _settings_from_conf_manager(session_key)
        except Exception:
            # Fall through to file-based read.
            pass
    return _settings_from_conf_files()


# ---------------------------------------------------------------------------
# Modular input
# ---------------------------------------------------------------------------

class AirspaceWatch(Script):

    def get_scheme(self) -> Scheme:
        scheme = Scheme("AirspaceWatch ADS-B Receiver")
        scheme.description = (
            "Polls a tar1090/dump1090 receiver every N seconds and emits one "
            "Splunk event per aircraft within the configured radius."
        )
        scheme.use_external_validation = True
        scheme.use_single_instance = False

        for arg in self._scheme_arguments():
            scheme.add_argument(arg)
        return scheme

    @staticmethod
    def _scheme_arguments() -> Iterable[Argument]:
        defs: List[Tuple[str, str, str, str]] = [
            # interval, index, sourcetype are Splunk system parameters; declaring
            # them as scheme arguments causes "internal argument" startup errors.
            ("adsb_host", "ADS-B host",
             "Hostname or IP of the receiver.", Argument.data_type_string),
            ("adsb_port", "ADS-B port",
             "TCP port for the receiver HTTP API.", Argument.data_type_number),
            ("adsb_path", "Aircraft JSON path",
             "HTTP path for aircraft JSON.", Argument.data_type_string),
            ("center_lat", "Centre latitude",
             "Centre latitude (degrees).", Argument.data_type_number),
            ("center_lon", "Centre longitude",
             "Centre longitude (degrees).", Argument.data_type_number),
            ("radius_nm", "Radius (nm)",
             "Radius from centre, in nautical miles.", Argument.data_type_number),
            ("request_timeout", "Request timeout (s)",
             "HTTP request timeout, in seconds.", Argument.data_type_number),
        ]
        for name, title, desc, dtype in defs:
            arg = Argument(name)
            arg.title = title
            arg.description = desc
            arg.data_type = dtype
            arg.required_on_create = False
            arg.required_on_edit = False
            yield arg

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _get(stanza: Dict[str, Any], key: str, default: Any) -> Any:
        """Pull a value from the input stanza, falling back to default."""
        value = stanza.get(key)
        if value in (None, ""):
            return default
        return value

    def _merged_config(
        self,
        input_stanza: Dict[str, Any],
        settings: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Per-input values override the Configuration-tab defaults."""
        receiver = settings.get("receiver", {})
        geofence = settings.get("geofence", {})
        return {
            "interval": float(self._get(input_stanza, "interval", 5)),
            "index": str(self._get(input_stanza, "index", "main")),
            "sourcetype": str(self._get(input_stanza, "sourcetype", "airspace:adsb")),
            "adsb_host": str(self._get(input_stanza, "adsb_host", receiver.get("adsb_host", "192.168.0.58"))),
            "adsb_port": int(float(self._get(input_stanza, "adsb_port", receiver.get("adsb_port", 8080)))),
            "adsb_path": str(self._get(input_stanza, "adsb_path", receiver.get("adsb_path", "/data/aircraft.json"))),
            "center_lat": float(self._get(input_stanza, "center_lat", geofence.get("center_lat", 53.8))),
            "center_lon": float(self._get(input_stanza, "center_lon", geofence.get("center_lon", -1.55))),
            "radius_nm": float(self._get(input_stanza, "radius_nm", geofence.get("radius_nm", 50))),
            "request_timeout": float(self._get(input_stanza, "request_timeout", receiver.get("request_timeout", 4))),
        }

    # ---- main loop ---------------------------------------------------------

    def stream_events(self, inputs, ew: EventWriter) -> None:  # type: ignore[override]
        session_key = getattr(self, "_input_definition", None)
        session_key = getattr(session_key, "metadata", {}).get("session_key") if session_key else None

        settings = load_settings(session_key)
        log_level = (settings.get("logging", {}) or {}).get("loglevel", "INFO")
        logger = _build_logger(log_level)

        for input_name, input_item in inputs.inputs.items():
            try:
                cfg = self._merged_config(input_item, settings)
            except (TypeError, ValueError) as exc:
                logger.error(
                    "[%s] invalid configuration: %s — input disabled for this cycle.",
                    input_name, exc,
                )
                continue

            logger.info(
                "[%s] poll host=%s:%s path=%s radius_nm=%s centre=(%s,%s) index=%s",
                input_name,
                cfg["adsb_host"], cfg["adsb_port"], cfg["adsb_path"],
                cfg["radius_nm"], cfg["center_lat"], cfg["center_lon"], cfg["index"],
            )

            try:
                payload = fetch_aircraft_json(
                    cfg["adsb_host"], cfg["adsb_port"], cfg["adsb_path"], cfg["request_timeout"],
                )
            except Exception as exc:
                logger.warning(
                    "[%s] fetch failed: %s — skipping this cycle.",
                    input_name, exc,
                )
                continue

            poll_time = float(payload.get("now") or time.time())
            aircraft = payload.get("aircraft") or []
            emitted = 0
            for ac in aircraft:
                try:
                    record = normalise_aircraft(
                        ac, poll_time, cfg["center_lat"], cfg["center_lon"], cfg["radius_nm"],
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "[%s] skipping aircraft %s: %s",
                        input_name, ac.get("hex"), exc,
                    )
                    continue
                if record is None:
                    continue
                event = Event()
                event.stanza = input_name
                event.sourcetype = cfg["sourcetype"]
                event.index = cfg["index"]
                event.host = cfg["adsb_host"]
                event.source = "airspace_watch://{}".format(input_name.split("://", 1)[-1])
                event.time = "{:.3f}".format(poll_time)
                event.data = json.dumps(record, separators=(",", ":"))
                ew.write_event(event)
                emitted += 1

            logger.info(
                "[%s] emitted=%d total_seen=%d", input_name, emitted, len(aircraft),
            )

    def validate_input(self, validation_definition) -> None:  # type: ignore[override]
        """App Inspect / Splunk Web calls this on create+edit."""
        params = validation_definition.parameters
        errors: List[str] = []

        try:
            interval = float(params.get("interval", 5))
            if interval < 1:
                errors.append("interval must be >= 1 second.")
        except (TypeError, ValueError):
            errors.append("interval must be numeric.")

        try:
            port = int(float(params.get("adsb_port", 8080)))
            if not 1 <= port <= 65535:
                errors.append("adsb_port must be between 1 and 65535.")
        except (TypeError, ValueError):
            errors.append("adsb_port must be numeric.")

        if "center_lat" in params:
            try:
                lat = float(params["center_lat"])
                if not -90 <= lat <= 90:
                    errors.append("center_lat must be between -90 and 90.")
            except (TypeError, ValueError):
                errors.append("center_lat must be numeric.")

        if "center_lon" in params:
            try:
                lon = float(params["center_lon"])
                if not -180 <= lon <= 180:
                    errors.append("center_lon must be between -180 and 180.")
            except (TypeError, ValueError):
                errors.append("center_lon must be numeric.")

        if "radius_nm" in params:
            try:
                r = float(params["radius_nm"])
                if r <= 0 or r > 500:
                    errors.append("radius_nm must be between 1 and 500.")
            except (TypeError, ValueError):
                errors.append("radius_nm must be numeric.")

        if errors:
            raise ValueError("; ".join(errors))


if __name__ == "__main__":
    try:
        sys.exit(AirspaceWatch().run(sys.argv))
    except Exception:  # pragma: no cover - last-chance traceback to splunkd.log
        sys.stderr.write(traceback.format_exc())
        sys.exit(1)

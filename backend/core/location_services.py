"""
OSC (One Stop Centre) Location Services
========================================
Finds the nearest One Stop Centres to a GPS location and fires
webhook notifications to them on emergency SOS — no government
app dependency.  Each OSC entry can optionally carry a webhook_url;
if absent the alert is stored in-memory and exposed via a polling
endpoint (/api/emergency/osc-alerts) so an OSC dashboard can pull it.

Upgrade path: just add "webhook_url" to an OSC entry in
sos_locations_india.json and it will start receiving HTTP POSTs
automatically — no other code change needed.
"""

from __future__ import annotations

import json
import os
import hashlib
import logging
from math import radians, sin, cos, sqrt, atan2
from typing import Optional, Dict, List
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field

import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# ── load location database ────────────────────────────────────────────────────
_LOC_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "sos_locations_india.json"
)
_RES_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "resources.json"
)

with open(_LOC_DB_PATH, "r", encoding="utf-8") as _f:
    _loc_data = json.load(_f)

with open(_RES_DB_PATH, "r", encoding="utf-8") as _f:
    _res_data = json.load(_f)

# index GPS-tagged OSCs
_GPS_OSCS: List[dict] = [
    loc for loc in _loc_data.get("locations", []) if loc.get("category") == "osc"
]

# build name→phone/open_hours lookup from resources.json for richer profiles
_RES_OSC_BY_DISTRICT: Dict[str, dict] = {}
for _osc in _res_data.get("one_stop_centres", []):
    _key = f"{_osc.get('district', '').lower()}:{_osc.get('state', '').lower()}"
    _RES_OSC_BY_DISTRICT.setdefault(_key, _osc)

print(
    f"Location Services ready. GPS-tagged OSCs: {len(_GPS_OSCS)}"
)


# ── in-memory alert log ───────────────────────────────────────────────────────
@dataclass
class OscAlert:
    alert_log_id:  str
    sos_alert_id:  str
    osc_id:        str
    osc_name:      str
    osc_phone:     str
    osc_address:   str
    district:      str
    state:         str
    distance_km:   float
    user_lat:      float
    user_lon:      float
    message:       str
    severity:      str
    maps_link:     str
    webhook_url:   Optional[str]
    webhook_status: str            # "sent" | "failed" | "no_webhook"
    webhook_response: Optional[str]
    sent_at:       str

    def to_dict(self) -> dict:
        return asdict(self)


# osc_id → list[OscAlert]
_osc_alert_log: Dict[str, List[OscAlert]] = {}
# sos_alert_id → list[OscAlert]  (reverse index)
_sos_alert_log: Dict[str, List[OscAlert]] = {}


# ── Haversine distance ────────────────────────────────────────────────────────
def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


# ── OSC finder ────────────────────────────────────────────────────────────────
def find_nearby_oscs(
    lat: float,
    lon: float,
    radius_km: float = 50.0,
    max_results: int = 3,
) -> List[dict]:
    """
    Return up to max_results OSCs within radius_km of (lat, lon),
    sorted by distance ascending.  Each entry is the raw location dict
    augmented with 'distance_km' and merged resource-db fields.
    """
    results = []
    for osc in _GPS_OSCS:
        dist = _haversine_km(lat, lon, osc["lat"], osc["lng"])
        if dist <= radius_km:
            entry = dict(osc)
            entry["distance_km"] = round(dist, 2)
            # merge richer info from resources.json
            key = f"{osc.get('district', '').lower()}:{osc.get('state', '').lower()}"
            res = _RES_OSC_BY_DISTRICT.get(key, {})
            entry.setdefault("phone",      res.get("phone", "181"))
            entry.setdefault("open_hours", res.get("open_hours", "24x7"))
            entry.setdefault("webhook_url", res.get("webhook_url"))  # None by default
            results.append(entry)

    results.sort(key=lambda x: x["distance_km"])
    return results[:max_results]


# ── webhook delivery ──────────────────────────────────────────────────────────
_WEBHOOK_TIMEOUT_SEC = 5
_WEBHOOK_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "SakhiBot-Emergency/1.0",
}


def _post_webhook(url: str, payload: dict) -> tuple[bool, str]:
    """
    Fire-and-forget HTTP POST to the OSC webhook.
    Returns (success: bool, response_text: str).
    """
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=_WEBHOOK_HEADERS, method="POST")
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_SEC) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return False, f"URLError: {e.reason}"
    except Exception as e:                          # noqa: BLE001
        return False, f"Error: {e}"


# ── notify single OSC ─────────────────────────────────────────────────────────
def _notify_osc(
    osc: dict,
    sos_alert_id: str,
    user_lat: float,
    user_lon: float,
    message: str,
    severity: str,
) -> OscAlert:
    """
    Build and deliver an OSC alert.  If a webhook_url is configured on the
    OSC entry, POST to it immediately.  Either way, store in-memory for
    polling via GET /api/emergency/osc-alerts.
    """
    now = datetime.now(timezone.utc).isoformat()
    maps_link = f"https://maps.google.com/?q={user_lat},{user_lon}"
    log_id = hashlib.sha256(
        f"{sos_alert_id}:{osc['id']}:{now}".encode()
    ).hexdigest()[:16]

    webhook_url = osc.get("webhook_url")
    webhook_status = "no_webhook"
    webhook_response = None

    if webhook_url:
        payload = {
            "event":        "EMERGENCY_SOS",
            "alert_id":     sos_alert_id,
            "severity":     severity,
            "message":      message,
            "user_location": {
                "latitude":  user_lat,
                "longitude": user_lon,
                "maps_link": maps_link,
            },
            "osc": {
                "id":       osc["id"],
                "name":     osc["name"],
                "district": osc.get("district", ""),
                "state":    osc.get("state", ""),
            },
            "distance_km":  osc["distance_km"],
            "timestamp":    now,
            "source":       "SakhiBot",
        }
        success, resp_text = _post_webhook(webhook_url, payload)
        webhook_status = "sent" if success else "failed"
        webhook_response = resp_text
        logger.info(
            "[OSC-NOTIFY] %s → %s (%s)",
            osc["name"], webhook_url, webhook_status
        )
    else:
        logger.info(
            "[OSC-NOTIFY] %s — no webhook configured, stored in log",
            osc["name"]
        )

    alert = OscAlert(
        alert_log_id=log_id,
        sos_alert_id=sos_alert_id,
        osc_id=osc["id"],
        osc_name=osc["name"],
        osc_phone=osc.get("phone", "181"),
        osc_address=osc.get("address", ""),
        district=osc.get("district", ""),
        state=osc.get("state", ""),
        distance_km=osc["distance_km"],
        user_lat=user_lat,
        user_lon=user_lon,
        message=message,
        severity=severity,
        maps_link=maps_link,
        webhook_url=webhook_url,
        webhook_status=webhook_status,
        webhook_response=webhook_response,
        sent_at=now,
    )

    _osc_alert_log.setdefault(osc["id"], []).append(alert)
    _sos_alert_log.setdefault(sos_alert_id, []).append(alert)
    return alert


# ── public orchestrator ───────────────────────────────────────────────────────
def notify_nearby_oscs(
    lat: float,
    lon: float,
    sos_alert_id: str,
    message: str,
    severity: str = "critical",
    radius_km: float = 50.0,
    max_oscs: int = 3,
) -> List[dict]:
    """
    Find and notify up to max_oscs nearby One Stop Centres.
    Returns a list of notification status dicts for inclusion in the API response.
    """
    nearby = find_nearby_oscs(lat, lon, radius_km=radius_km, max_results=max_oscs)
    results = []
    for osc in nearby:
        alert = _notify_osc(
            osc=osc,
            sos_alert_id=sos_alert_id,
            user_lat=lat,
            user_lon=lon,
            message=message,
            severity=severity,
        )
        results.append({
            "osc_id":         alert.osc_id,
            "osc_name":       alert.osc_name,
            "osc_phone":      alert.osc_phone,
            "address":        alert.osc_address,
            "district":       alert.district,
            "state":          alert.state,
            "distance_km":    alert.distance_km,
            "webhook_status": alert.webhook_status,
            "maps_link":      alert.maps_link,
        })

    if not results:
        logger.warning(
            "[OSC-NOTIFY] No OSCs found within %.0f km of (%.4f, %.4f)",
            radius_km, lat, lon
        )
    return results


# ── polling helpers ───────────────────────────────────────────────────────────
def get_osc_alert_log(osc_id: Optional[str] = None) -> List[dict]:
    """
    Return stored OSC alerts.
    - osc_id=None  → all alerts across all OSCs
    - osc_id=<id>  → alerts for that specific OSC
    """
    if osc_id:
        return [a.to_dict() for a in _osc_alert_log.get(osc_id, [])]

    all_alerts = []
    for alerts in _osc_alert_log.values():
        all_alerts.extend(a.to_dict() for a in alerts)
    all_alerts.sort(key=lambda a: a["sent_at"], reverse=True)
    return all_alerts


def get_alerts_by_sos(sos_alert_id: str) -> List[dict]:
    """Return all OSC notifications triggered by a specific SOS alert."""
    return [a.to_dict() for a in _sos_alert_log.get(sos_alert_id, [])]

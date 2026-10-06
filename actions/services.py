"""
services.py - framework-independent business logic for the Eco-Travel Advisor.

Keeping API calls and scoring here (instead of inside Rasa Action classes)
means every function can be unit-tested with pytest without starting Rasa.

Every external call:
  * has a short timeout (keeps total response < 3 s),
  * catches network / HTTP / JSON errors,
  * falls back to local data so the user always gets an answer.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

API_TIMEOUT = 2.0  # seconds - leaves headroom under the 3 s latency target
MOCK_PATH = Path(__file__).resolve().parent.parent / "mock_data" / "eco_hotels.json"

# ---------------------------------------------------------------------------
# 1. Fallback emission factors (kg CO2e per passenger-km).
#    Used only if Climatiq is unreachable. Approximate public averages.
# ---------------------------------------------------------------------------
FALLBACK_FACTORS = {
    "flight": 0.246,  # short-haul incl. radiative forcing
    "car": 0.171,     # average petrol car, single occupant
    "bus": 0.027,     # long-distance coach
    "train": 0.035,   # international rail average
}

# Climatiq activity IDs per transport mode (check the Climatiq data explorer
# for the current data_version before deployment).
CLIMATIQ_ACTIVITY_IDS = {
    "flight": "passenger_flight-route_type_international-aircraft_type_na-"
              "distance_short_haul_lt_3700km-class_na-rf_included-distance_uplift_na",
    "car": "passenger_vehicle-vehicle_type_car-fuel_source_na-engine_size_na-"
           "vehicle_age_na-vehicle_weight_na",
    "bus": "passenger_vehicle-vehicle_type_coach-fuel_source_na-distance_na-"
           "engine_size_na",
    "train": "passenger_train-route_type_international_rail-fuel_source_na",
}

# Small coordinate table so distance still works without OpenCage.
CITY_COORDS = {
    "bucharest": (44.43, 26.10), "lisbon": (38.72, -9.14),
    "berlin": (52.52, 13.40), "vienna": (48.21, 16.37),
    "amsterdam": (52.37, 4.90), "munich": (48.14, 11.58),
    "rural": (45.60, 25.30),  # Carpathian region placeholder
}


# ---------------------------------------------------------------------------
# 2. Geography helpers
# ---------------------------------------------------------------------------
def haversine_km(a: tuple, b: tuple) -> float:
    """Great-circle distance between two (lat, lon) points in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def geocode(place: str) -> Optional[tuple]:
    """Resolve a place name with OpenCage; fall back to the local table.
    Also accepts raw GPS input "lat,lon" sent by the frontend location button."""
    try:
        lat, lon = (float(x) for x in str(place).split(","))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    except ValueError:
        pass
    key = os.getenv("OPENCAGE_API_KEY")
    if key:
        try:
            r = requests.get(
                "https://api.opencagedata.com/geocode/v1/json",
                params={"q": place, "key": key, "limit": 1, "no_annotations": 1},
                timeout=API_TIMEOUT,
            )
            r.raise_for_status()
            g = r.json()["results"][0]["geometry"]
            return g["lat"], g["lng"]
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            logger.warning("OpenCage failed for %s: %s", place, e)
    return CITY_COORDS.get(place.lower().strip()) if place else None


# ---------------------------------------------------------------------------
# 3. Carbon estimation (Climatiq with fallback)
# ---------------------------------------------------------------------------
def climatiq_estimate(mode: str, distance_km: float, passengers: int = 1) -> Dict:
    """
    Return {"mode", "kgco2e", "source"} for one transport mode.
    source == "climatiq" for live data, "fallback" for local factors.
    """
    key = os.getenv("CLIMATIQ_API_KEY")
    if key and mode in CLIMATIQ_ACTIVITY_IDS:
        body = {
            "emission_factor": {"activity_id": CLIMATIQ_ACTIVITY_IDS[mode],
                                "data_version": "^21"},
            "parameters": {"passengers": passengers, "distance": distance_km,
                           "distance_unit": "km"},
        }
        try:
            r = requests.post("https://api.climatiq.io/data/v1/estimate",
                              headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=API_TIMEOUT)
            r.raise_for_status()
            return {"mode": mode, "kgco2e": round(r.json()["co2e"], 1),
                    "source": "climatiq"}
        except (requests.RequestException, KeyError, ValueError) as e:
            logger.warning("Climatiq failed for %s: %s", mode, e)
    factor = FALLBACK_FACTORS.get(mode, FALLBACK_FACTORS["car"])
    return {"mode": mode, "kgco2e": round(factor * distance_km * passengers, 1),
            "source": "fallback"}


def emission_band(kgco2e: float) -> str:
    """Map one-way kg CO2e per passenger to the UI colour band."""
    if kgco2e < 50:
        return "green"
    if kgco2e <= 150:
        return "amber"
    return "red"


# ---------------------------------------------------------------------------
# 4. Amadeus sandbox (hotels / flights) with mock fallback
# ---------------------------------------------------------------------------
AMADEUS_BASE = "https://test.api.amadeus.com"


def _amadeus_token() -> Optional[str]:
    cid, secret = os.getenv("AMADEUS_CLIENT_ID"), os.getenv("AMADEUS_CLIENT_SECRET")
    if not (cid and secret):
        return None
    try:
        r = requests.post(f"{AMADEUS_BASE}/v1/security/oauth2/token",
                          data={"grant_type": "client_credentials",
                                "client_id": cid, "client_secret": secret},
                          timeout=API_TIMEOUT)
        r.raise_for_status()
        return r.json()["access_token"]
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning("Amadeus auth failed: %s", e)
        return None


def load_mock() -> Dict:
    with open(MOCK_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch_hotels(city: str, city_code: Optional[str] = None) -> List[Dict]:
    """
    Merge Amadeus hotel listings with the curated eco-certification file.
    Only hotels present in the curated file are labelled 'certified', so the
    bot never invents an environmental claim (anti-greenwashing rule).
    """
    curated = [h for h in load_mock()["hotels"]
               if h["city"].lower() == (city or "").lower()]
    token = _amadeus_token()
    if token and city_code:
        try:
            r = requests.get(
                f"{AMADEUS_BASE}/v1/reference-data/locations/hotels/by-city",
                headers={"Authorization": f"Bearer {token}"},
                params={"cityCode": city_code, "radius": 5},
                timeout=API_TIMEOUT)
            r.raise_for_status()
            names = {h["name"].lower() for h in curated}
            for h in r.json().get("data", [])[:5]:
                if h["name"].lower() not in names:
                    curated.append({"name": h["name"].title(), "city": city,
                                    "price_per_night": None,
                                    "certification": None,
                                    "kgco2_per_night": 15.0})
        except (requests.RequestException, KeyError, ValueError) as e:
            logger.warning("Amadeus hotels failed: %s", e)
    return curated


def fetch_flight_price(origin_code: str, dest_code: str, date: str) -> Optional[float]:
    """
    Cheapest one-way economy fare (EUR) from the Amadeus Flight Offers sandbox.
    Returns None on any failure so callers fall back to indicative fares.
    `date` must be ISO format YYYY-MM-DD.
    """
    token = _amadeus_token()
    if not (token and origin_code and dest_code and date):
        return None
    try:
        r = requests.get(
            f"{AMADEUS_BASE}/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {token}"},
            params={"originLocationCode": origin_code,
                    "destinationLocationCode": dest_code,
                    "departureDate": date, "adults": 1, "max": 5,
                    "currencyCode": "EUR"},
            timeout=API_TIMEOUT)
        r.raise_for_status()
        prices = [float(o["price"]["grandTotal"]) for o in r.json().get("data", [])]
        return min(prices) if prices else None
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning("Amadeus flights failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# 5. Weighted scoring
# ---------------------------------------------------------------------------
WEIGHTS = {  # (carbon, price, preference) - sum to 1
    "high": (0.60, 0.25, 0.15),
    "medium": (0.40, 0.40, 0.20),
    "low": (0.20, 0.60, 0.20),
}


def _norm(values: List[float]) -> List[float]:
    """Min-max normalise to 0..1 (all equal -> 0)."""
    lo, hi = min(values), max(values)
    return [0.0 if hi == lo else (v - lo) / (hi - lo) for v in values]


def rank_options(options: List[Dict], level: str = "medium",
                 budget: Optional[float] = None) -> List[Dict]:
    """
    score = w_c*(1-carbon_norm) + w_p*(1-price_norm) + w_pref*pref_match
    pref_match = 1 if the option is certified / low-carbon mode, else 0.
    Options above budget are kept but flagged and penalised by 0.2.
    """
    if not options:
        return []
    wc, wp, wf = WEIGHTS.get(level, WEIGHTS["medium"])
    carbons = _norm([o["kgco2e"] for o in options])
    prices = _norm([o["price"] for o in options])
    ranked = []
    for o, c, p in zip(options, carbons, prices):
        pref = 1.0 if o.get("certified") or o.get("mode") in ("train", "bus") else 0.0
        score = wc * (1 - c) + wp * (1 - p) + wf * pref
        over = budget is not None and o["price"] > budget
        if over:
            score -= 0.2
        ranked.append({**o, "score": round(score, 3), "over_budget": over,
                       "band": emission_band(o["kgco2e"])})
    return sorted(ranked, key=lambda x: x["score"], reverse=True)


# ---------------------------------------------------------------------------
# 6. Handover packaging (GDPR data-minimised)
# ---------------------------------------------------------------------------
def build_handover_payload(slots: Dict, transcript: List[Dict],
                           reason: str, sender_id: str) -> Dict:
    """Bundle everything an advisor needs; drop empty slots, hash sender id."""
    import hashlib
    from datetime import datetime, timezone
    return {
        "session_ref": hashlib.sha256(sender_id.encode()).hexdigest()[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "trip": {k: v for k, v in slots.items() if v not in (None, "", [])},
        "transcript": transcript[-30:],  # last 30 turns only
    }

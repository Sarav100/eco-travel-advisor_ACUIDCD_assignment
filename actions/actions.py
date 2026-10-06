"""
actions.py - Rasa custom actions for the Eco-Travel Advisor.

Each Action is a thin wrapper: it reads slots, calls services.py, and turns
the result into bot messages / custom JSON for the frontend. All failures
are caught so the user always receives a fallback message.

Run with:  rasa run actions --port 5055
"""
import logging
import os
from typing import Any, Dict, List, Text

import requests
from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import ConversationPaused, SlotSet
from rasa_sdk.executor import CollectingDispatcher

from . import services

logger = logging.getLogger(__name__)
MODES = ["train", "bus", "car", "flight"]
CITY_CODES = {"lisbon": "LIS", "berlin": "BER", "vienna": "VIE",
              "amsterdam": "AMS", "munich": "MUC", "bucharest": "OTP"}


class ValidateTripForm(FormValidationAction):
    """Error recovery inside the intake form (ambiguous / invalid values)."""

    def name(self) -> Text:
        return "validate_trip_form"

    def validate_budget(self, value, dispatcher, tracker, domain):
        try:
            amount = float(str(value).replace("€", "").replace(",", ""))
            if amount <= 0:
                raise ValueError
            return {"budget": amount}
        except (TypeError, ValueError):
            dispatcher.utter_message(text="Please give the budget as a number, e.g. 800.")
            return {"budget": None}

    def validate_destination(self, value, dispatcher, tracker, domain):
        if services.geocode(value) is None:
            dispatcher.utter_message(
                text=f"I couldn't find '{value}'. Pick one or type another city:",
                buttons=[{"title": c.title(), "payload":
                          f'/inform_destination{{"destination":"{c}"}}'}
                         for c in ("lisbon", "berlin", "vienna")])
            return {"destination": None}
        return {"destination": value}


class ActionSearchOptions(Action):
    """Fetch hotels (Amadeus + curated eco file)."""

    def name(self) -> Text:
        return "action_search_options"

    def run(self, dispatcher, tracker, domain):
        city = tracker.get_slot("destination") or ""
        try:
            hotels = services.fetch_hotels(city, CITY_CODES.get(city.lower()))
        except Exception as e:  # last-resort guard
            logger.exception("hotel search crashed: %s", e)
            hotels = []
        if not hotels:
            dispatcher.utter_message(text="I couldn't retrieve hotels right now - "
                                          "I'll still compare transport options.")
            return []
        # Carousel sent as custom JSON; frontend renders cards.
        dispatcher.utter_message(json_message={"type": "hotel_carousel", "items": [
            {"title": h["name"], "price": h["price_per_night"],
             "badge": h["certification"] or "Not verified",
             "kgco2_per_night": h["kgco2_per_night"]} for h in hotels]})
        return []


class ActionCarbonEstimate(Action):
    """Compare per-mode CO2e via Climatiq and emit colour-coded cards."""

    def name(self) -> Text:
        return "action_carbon_estimate"

    def run(self, dispatcher, tracker, domain):
        origin = tracker.get_slot("origin") or os.getenv("DEFAULT_ORIGIN", "Bucharest")
        dest = tracker.get_slot("destination")
        a, b = services.geocode(origin), services.geocode(dest or "")
        if not (a and b):
            dispatcher.utter_message(text="I need a valid origin and destination "
                                          "to estimate emissions.")
            return []
        km = services.haversine_km(a, b)
        results = [services.climatiq_estimate(m, km) for m in MODES]
        if any(r["source"] == "fallback" for r in results):
            dispatcher.utter_message(text="⚠️ Live carbon data unavailable - "
                                          "showing average emission factors.")
        cards = [{**r, "band": services.emission_band(r["kgco2e"])} for r in results]
        dispatcher.utter_message(json_message={"type": "carbon_cards",
                                               "distance_km": round(km), "items": cards})
        if any(c["band"] == "red" for c in cards):
            dispatcher.utter_message(text="🔴 Flying is the highest-emission option "
                                          "for this route. Rail cuts emissions by "
                                          "roughly 80-90%.")
        return []


class ActionRankOptions(Action):
    """Weighted ranking of transport options + dynamic quick replies."""

    def name(self) -> Text:
        return "action_rank_options"

    def run(self, dispatcher, tracker, domain):
        level = tracker.get_slot("sustainability_level") or "medium"
        budget = tracker.get_slot("budget")
        origin = tracker.get_slot("origin") or os.getenv("DEFAULT_ORIGIN", "Bucharest")
        a = services.geocode(origin)
        b = services.geocode(tracker.get_slot("destination") or "")
        if not (a and b):
            return []
        km = services.haversine_km(a, b)
        # Indicative fares (€/km) when live fares are unavailable.
        fare = {"train": 0.11, "bus": 0.06, "car": 0.14, "flight": 0.08}
        opts = [{**services.climatiq_estimate(m, km), "price": round(fare[m] * km)}
                for m in MODES]
        # Replace the indicative air fare with a live Amadeus fare when possible.
        # travel_dates is expected to start with an ISO date (YYYY-MM-DD).
        dates = (tracker.get_slot("travel_dates") or "")[:10]
        live = services.fetch_flight_price(
            CITY_CODES.get(origin.lower(), ""),
            CITY_CODES.get((tracker.get_slot("destination") or "").lower(), ""), dates)
        if live:
            for o in opts:
                if o["mode"] == "flight":
                    o["price"] = round(live)
        ranked = services.rank_options(opts, level, budget)
        best = ranked[0]
        dispatcher.utter_message(
            text=f"Best match for '{level}' sustainability: {best['mode']} "
                 f"(~{best['kgco2e']} kg CO2e, ~€{best['price']}).",
            buttons=[{"title": f"{o['mode'].title()} · {o['kgco2e']} kg",
                      "payload": f"/affirm{{\"transport_mode\":\"{o['mode']}\"}}"}
                     for o in ranked] +
                    [{"title": "Talk to an advisor", "payload": "/request_human"}])
        return []


class ActionTwoStageFallback(Action):
    """Stage 1: rephrase. Stage 2: constrained buttons. Stage 3: human."""

    def name(self) -> Text:
        return "action_two_stage_fallback"

    def run(self, dispatcher, tracker, domain):
        count = int(tracker.get_slot("fallback_count") or 0)
        if count == 0:
            dispatcher.utter_message(response="utter_clarify_stage1")
        elif count == 1:
            dispatcher.utter_message(response="utter_clarify_stage2")
        else:
            return ActionHandoverToHuman().run(dispatcher, tracker, domain,
                                               reason="repeated_misunderstanding")
        return [SlotSet("fallback_count", count + 1)]


class ActionDefaultFallback(ActionTwoStageFallback):
    """Core-level fallback re-uses the same two-stage logic."""

    def name(self) -> Text:
        return "action_default_fallback"


class ActionHandoverToHuman(Action):
    """Package context, notify advisor webhook, pause the bot."""

    def name(self) -> Text:
        return "action_handover_to_human"

    def run(self, dispatcher, tracker, domain, reason: str = "user_request"):
        slots = {k: tracker.get_slot(k) for k in
                 ("destination", "origin", "travel_dates", "budget",
                  "sustainability_level")}
        transcript = [{"from": e["event"], "text": e.get("text")}
                      for e in tracker.events
                      if e["event"] in ("user", "bot") and e.get("text")]
        payload = services.build_handover_payload(slots, transcript, reason,
                                                  tracker.sender_id)
        url = os.getenv("ADVISOR_WEBHOOK_URL")
        delivered = False
        if url:
            try:
                requests.post(url, json=payload, timeout=services.API_TIMEOUT).raise_for_status()
                delivered = True
            except requests.RequestException as e:
                logger.error("Advisor webhook failed: %s", e)
        else:
            logger.info("HANDOVER (no webhook configured): %s", payload)
        dispatcher.utter_message(json_message={"type": "handover", "active": True,
                                               "ref": payload["session_ref"]})
        dispatcher.utter_message(
            text="👤 You're being connected to a human travel advisor. They can "
                 "see your trip details, so you won't need to repeat yourself."
            if delivered or not url else
            "👤 Our advisors are unavailable right now. Your reference is "
            f"{payload['session_ref']} - please email advisors@ecotrip.example.")
        return [SlotSet("handover_active", True), ConversationPaused()]

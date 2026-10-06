"""Unit tests for services.py - run with:  pytest -v tests/test_services.py
All external APIs are mocked, so no keys or network are needed."""
from unittest.mock import MagicMock, patch

import requests

from actions import services


def test_haversine_bucharest_vienna():
    km = services.haversine_km(services.CITY_COORDS["bucharest"],
                               services.CITY_COORDS["vienna"])
    assert 840 < km < 870


def test_emission_bands():
    assert services.emission_band(20) == "green"
    assert services.emission_band(100) == "amber"
    assert services.emission_band(300) == "red"


@patch.dict("os.environ", {"CLIMATIQ_API_KEY": "test"})
@patch("actions.services.requests.post")
def test_climatiq_success(mock_post):
    mock_post.return_value = MagicMock(status_code=200,
                                       json=lambda: {"co2e": 29.4})
    mock_post.return_value.raise_for_status = lambda: None
    r = services.climatiq_estimate("train", 850)
    assert r == {"mode": "train", "kgco2e": 29.4, "source": "climatiq"}


@patch.dict("os.environ", {"CLIMATIQ_API_KEY": "test"})
@patch("actions.services.requests.post", side_effect=requests.Timeout)
def test_climatiq_timeout_uses_fallback(_):
    r = services.climatiq_estimate("flight", 1000)
    assert r["source"] == "fallback" and r["kgco2e"] == 246.0


@patch.dict("os.environ", {"CLIMATIQ_API_KEY": "test"})
@patch("actions.services.requests.post")
def test_climatiq_malformed_json_uses_fallback(mock_post):
    mock_post.return_value = MagicMock(json=lambda: {"unexpected": 1})
    mock_post.return_value.raise_for_status = lambda: None
    assert services.climatiq_estimate("bus", 100)["source"] == "fallback"


def test_rank_high_prefers_train():
    opts = [{"mode": "flight", "kgco2e": 210, "price": 70},
            {"mode": "train", "kgco2e": 30, "price": 95}]
    assert services.rank_options(opts, "high")[0]["mode"] == "train"


def test_rank_low_prefers_cheap():
    opts = [{"mode": "flight", "kgco2e": 210, "price": 40},
            {"mode": "car", "kgco2e": 150, "price": 160}]
    assert services.rank_options(opts, "low")[0]["mode"] == "flight"


def test_rank_flags_over_budget():
    opts = [{"mode": "train", "kgco2e": 30, "price": 900}]
    assert services.rank_options(opts, "high", budget=500)[0]["over_budget"]


def test_rank_empty():
    assert services.rank_options([]) == []


def test_fetch_hotels_without_keys_uses_curated(monkeypatch):
    monkeypatch.delenv("AMADEUS_CLIENT_ID", raising=False)
    hotels = services.fetch_hotels("Lisbon", "LIS")
    assert hotels and all(h["certification"] for h in hotels)


def test_geocode_unknown_city_returns_none(monkeypatch):
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    assert services.geocode("Atlantis") is None


def test_handover_payload_minimised():
    p = services.build_handover_payload(
        {"destination": "Lisbon", "budget": None}, [{"from": "user", "text": "hi"}],
        "user_request", "alice@example.com")
    assert "budget" not in p["trip"]
    assert "alice" not in p["session_ref"] and len(p["session_ref"]) == 12


def test_geocode_accepts_gps_string():
    assert services.geocode("44.43,26.10") == (44.43, 26.10)


@patch("actions.services._amadeus_token", return_value="tok")
@patch("actions.services.requests.get")
def test_flight_price_cheapest(mock_get, _):
    mock_get.return_value = MagicMock(json=lambda: {"data": [
        {"price": {"grandTotal": "120.50"}}, {"price": {"grandTotal": "89.99"}}]})
    mock_get.return_value.raise_for_status = lambda: None
    assert services.fetch_flight_price("OTP", "VIE", "2026-05-12") == 89.99


@patch("actions.services._amadeus_token", return_value="tok")
@patch("actions.services.requests.get", side_effect=requests.ConnectionError)
def test_flight_price_failure_returns_none(*_):
    assert services.fetch_flight_price("OTP", "VIE", "2026-05-12") is None

from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.actions import handlers
from app.actions.handlers import eBirdObservation


def _observation_payload(**overrides):
    payload = {
        "speciesCode": "tstbrd",
        "comName": "Test Bird",
        "sciName": "Avium testus",
        "locId": "L123",
        "locName": "Test Park",
        "obsDt": "2026-08-09 14:32",
        "howMany": 3,
        "lat": 12.34,
        "lng": 56.78,
        "obsValid": True,
        "obsReviewed": False,
        "locationPrivate": False,
        "subId": "S-1",
    }
    payload.update(overrides)
    return payload


def test_obsDt_with_time_parses_as_utc():
    obs = eBirdObservation.parse_obj(_observation_payload(obsDt="2026-08-09 14:32"))
    assert obs.obsDt == datetime(2026, 8, 9, 14, 32, tzinfo=timezone.utc)


def test_obsDt_date_only_parses_as_midnight_utc():
    # eBird omits the time when a checklist has no start time.
    obs = eBirdObservation.parse_obj(_observation_payload(obsDt="2026-08-09"))
    assert obs.obsDt == datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)


def test_transform_ebird_to_gundi_event_creates_expected_structure():
    # Build a minimal object that mimics eBirdObservation attributes
    obs = SimpleNamespace(
        comName="Test Bird",
        sciName="Avium testus",
        speciesCode="tstbrd",
        locId="L123",
        locName="Test Park",
        obsDt=datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),  # timezone-aware datetime (UTC)
        howMany=3,
        lat=12.34,
        lng=56.78,
        obsValid=True,
        obsReviewed=False,
        locationPrivate=False,
        subId="S-1",
    )

    event = handlers._transform_ebird_to_gundi_event(obs)

    assert event["title"] == "Test Bird observation"
    assert event["event_type"] == "ebird_observation"
    assert event["recorded_at"] == datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()
    assert event["location"] == {"lat": 12.34, "lon": 56.78}
    details = event["event_details"]
    assert details["common_name"] == "Test Bird"
    assert details["scientific_name"] == "Avium testus"
    assert details["species_code"] == "tstbrd"
    assert details["location_id"] == "L123"
    assert details["location_name"] == "Test Park"
    assert details["quantity"] == 3
    assert details["valid"] is True
    assert details["reviewed"] is False
    assert details["submission_id"] == "S-1"


def test_transform_preserves_timezone_aware_obsDt():
    aware_dt = datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)
    obs = SimpleNamespace(
        comName="Aware Bird",
        sciName="Aware avium",
        speciesCode="awr1",
        locId="L777",
        locName="Aware Park",
        obsDt=aware_dt,
        howMany=1,
        lat=0.0,
        lng=0.0,
        obsValid=True,
        obsReviewed=True,
        locationPrivate=False,
        subId="SUB",
    )

    event = handlers._transform_ebird_to_gundi_event(obs)
    assert event["recorded_at"] == aware_dt.isoformat()
    assert event["location"] == {"lat": 0.0, "lon": 0.0}
    details = event["event_details"]
    assert details["quantity"] == 1
    assert details["submission_id"] == "SUB"

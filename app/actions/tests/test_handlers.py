from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.actions import handlers


@pytest.mark.asyncio
async def test_filter_ebird_events_no_saved_state_returns_all(monkeypatch):
    async def fake_get_state(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers.state_manager, "get_state", fake_get_state)

    events = [
        {"recorded_at": datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)},
        {"recorded_at": datetime(2023, 1, 1, 11, 0, tzinfo=timezone.utc)},
    ]

    result = await handlers.filter_ebird_events("integration-id", events)
    assert result == events


@pytest.mark.asyncio
async def test_filter_ebird_events_with_iso_saved_state_filters(monkeypatch):
    async def fake_get_state(*args, **kwargs):
        return {"latest_observation_datetime": "2023-01-01T11:30:00+00:00"}

    monkeypatch.setattr(handlers.state_manager, "get_state", fake_get_state)

    event_1 = {"recorded_at": datetime(2023, 1, 1, 11, 0, tzinfo=timezone.utc)}
    event_2 = {"recorded_at": datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)}
    # Events before filtering
    events = [event_1, event_2]

    result = await handlers.filter_ebird_events("integration-id", events)

    # Events after filtering
    assert result == [event_2]


def test_transform_ebird_to_gundi_event_creates_expected_structure():
    # Build a minimal object that mimics eBirdObservation attributes
    obs = SimpleNamespace(
        comName="Test Bird",
        sciName="Avium testus",
        speciesCode="tstbrd",
        locId="L123",
        locName="Test Park",
        obsDt=datetime(2023, 1, 1, 12, 0),  # naive datetime; transform should add UTC tz
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
    assert event["recorded_at"] == datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)
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

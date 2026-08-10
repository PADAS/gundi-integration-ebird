from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.actions import handlers
from app.actions.configurations import PullEventsConfig, SearchParameter
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


def _make_integration():
    return SimpleNamespace(
        id="e9c1eef0-7c28-46bb-8155-fe9b31dedce7",
        base_url=None,
        configurations=[
            SimpleNamespace(action=SimpleNamespace(value="auth"), data={"api_key": "test-key"}),
        ],
    )


def _make_config(num_days=5):
    return PullEventsConfig(
        search_parameter=SearchParameter.REGION,
        region_code="US-CA",
        num_days=num_days,
    )


@pytest.fixture
def sync_mocks(monkeypatch):
    """Mock the eBird API, Gundi senders, and state manager around action_pull_events."""
    mocks = SimpleNamespace(
        ebird=AsyncMock(return_value=[]),
        send=AsyncMock(side_effect=lambda events, **kw: [{"object_id": f"gid-{i}"} for i in range(len(events))]),
        update=AsyncMock(return_value={}),
        get_state=AsyncMock(return_value=None),
        set_state=AsyncMock(return_value=None),
    )
    monkeypatch.setattr("app.services.activity_logger.publish_event", AsyncMock())
    monkeypatch.setattr(handlers, "_get_from_ebird", mocks.ebird)
    monkeypatch.setattr(handlers, "send_events_to_gundi", mocks.send)
    monkeypatch.setattr(handlers, "update_event_in_gundi", mocks.update, raising=False)
    monkeypatch.setattr(handlers.state_manager, "get_state", mocks.get_state)
    monkeypatch.setattr(handlers.state_manager, "set_state", mocks.set_state)
    return mocks


def _saved_state(mocks):
    assert mocks.set_state.await_count >= 1
    return mocks.set_state.await_args.args[2]


@pytest.mark.asyncio
async def test_new_observations_are_sent_and_recorded(sync_mocks):
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt="2026-08-09 10:00"),
        _observation_payload(subId="S-1", speciesCode="sp2", obsDt="2026-08-09 11:00"),
    ]

    result = await handlers.action_pull_events(_make_integration(), _make_config())

    assert result["result"]["events_extracted"] == 2
    sent_events = sync_mocks.send.await_args.kwargs.get("events") or sync_mocks.send.await_args.args[0]
    assert len(sent_events) == 2
    state = _saved_state(sync_mocks)
    assert state["observations"]["S-1:sp1"]["gundi_event_id"] == "gid-0"
    assert state["observations"]["S-1:sp2"]["gundi_event_id"] == "gid-1"


@pytest.mark.asyncio
async def test_unchanged_observations_are_not_resent(sync_mocks):
    obs = _observation_payload(subId="S-1", speciesCode="sp1", obsDt="2026-08-09 10:00")
    sync_mocks.ebird.return_value = [obs]
    await handlers.action_pull_events(_make_integration(), _make_config())
    first_state = _saved_state(sync_mocks)

    sync_mocks.get_state.return_value = first_state
    sync_mocks.send.reset_mock()
    result = await handlers.action_pull_events(_make_integration(), _make_config())

    assert result["result"]["events_extracted"] == 0
    sync_mocks.send.assert_not_awaited()
    sync_mocks.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_edited_observation_updates_existing_event(sync_mocks):
    original = _observation_payload(subId="S-1", speciesCode="sp1", obsDt="2026-08-09 10:00", howMany=3)
    sync_mocks.ebird.return_value = [original]
    await handlers.action_pull_events(_make_integration(), _make_config())
    first_state = _saved_state(sync_mocks)

    edited = dict(original, howMany=7)
    sync_mocks.ebird.return_value = [edited]
    sync_mocks.get_state.return_value = first_state
    sync_mocks.send.reset_mock()
    result = await handlers.action_pull_events(_make_integration(), _make_config())

    sync_mocks.send.assert_not_awaited()
    sync_mocks.update.assert_awaited_once()
    update_kwargs = sync_mocks.update.await_args.kwargs
    assert update_kwargs["event_id"] == "gid-0"
    assert update_kwargs["event"]["event_details"]["quantity"] == 7
    assert result["result"]["events_updated"] == 1


@pytest.mark.asyncio
async def test_late_submitted_observation_is_delivered(sync_mocks):
    # First run sees a recent observation; second run surfaces a checklist
    # observed EARLIER but submitted late — the old watermark logic dropped it.
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt="2026-08-09 10:00"),
    ]
    await handlers.action_pull_events(_make_integration(), _make_config())
    first_state = _saved_state(sync_mocks)

    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt="2026-08-09 10:00"),
        _observation_payload(subId="S-2", speciesCode="sp1", obsDt="2026-08-07 09:00"),
    ]
    sync_mocks.get_state.return_value = first_state
    sync_mocks.send.reset_mock()
    result = await handlers.action_pull_events(_make_integration(), _make_config())

    assert result["result"]["events_extracted"] == 1
    sent_events = sync_mocks.send.await_args.kwargs.get("events") or sync_mocks.send.await_args.args[0]
    assert sent_events[0]["event_details"]["submission_id"] == "S-2"


@pytest.mark.asyncio
async def test_date_only_observation_is_delivered(sync_mocks):
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt="2026-08-09 10:00"),
    ]
    await handlers.action_pull_events(_make_integration(), _make_config())
    first_state = _saved_state(sync_mocks)

    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt="2026-08-09 10:00"),
        _observation_payload(subId="S-3", speciesCode="sp1", obsDt="2026-08-09"),
    ]
    sync_mocks.get_state.return_value = first_state
    sync_mocks.send.reset_mock()
    result = await handlers.action_pull_events(_make_integration(), _make_config())

    assert result["result"]["events_extracted"] == 1
    sent_events = sync_mocks.send.await_args.kwargs.get("events") or sync_mocks.send.await_args.args[0]
    assert sent_events[0]["event_details"]["submission_id"] == "S-3"


@pytest.mark.asyncio
async def test_legacy_watermark_state_seeds_without_resending(sync_mocks):
    # Old-format state (watermark only). Observations at/before the watermark were
    # already sent by the old logic — record them without resending; newer ones send.
    sync_mocks.get_state.return_value = {"latest_observation_at": "2026-08-09T10:00:00+00:00"}
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-OLD", speciesCode="sp1", obsDt="2026-08-09 09:00"),
        _observation_payload(subId="S-NEW", speciesCode="sp1", obsDt="2026-08-09 11:00"),
    ]

    result = await handlers.action_pull_events(_make_integration(), _make_config())

    assert result["result"]["events_extracted"] == 1
    sent_events = sync_mocks.send.await_args.kwargs.get("events") or sync_mocks.send.await_args.args[0]
    assert sent_events[0]["event_details"]["submission_id"] == "S-NEW"
    state = _saved_state(sync_mocks)
    assert state["observations"]["S-OLD:sp1"]["gundi_event_id"] is None
    assert state["observations"]["S-NEW:sp1"]["gundi_event_id"] == "gid-0"


@pytest.mark.asyncio
async def test_empty_pruned_state_is_not_treated_as_legacy(sync_mocks):
    # New-format state whose observation map emptied out (quiet region, or
    # num_days raised after pruning) must not seed-skip like legacy state:
    # a late submission older than the stored watermark must still be sent.
    sync_mocks.get_state.return_value = {
        "latest_observation_at": "2026-08-09T10:00:00+00:00",
        "observations": {},
    }
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-LATE", speciesCode="sp1", obsDt="2026-08-08 09:00"),
    ]

    result = await handlers.action_pull_events(_make_integration(), _make_config())

    assert result["result"]["events_extracted"] == 1
    state = _saved_state(sync_mocks)
    assert state["observations"]["S-LATE:sp1"]["gundi_event_id"] == "gid-0"


@pytest.mark.asyncio
async def test_malformed_record_is_skipped_without_aborting(sync_mocks):
    bad = _observation_payload(subId="S-BAD", speciesCode="sp1")
    del bad["lat"]
    sync_mocks.ebird.return_value = [
        bad,
        _observation_payload(subId="S-GOOD", speciesCode="sp1", obsDt="2026-08-09 10:00"),
    ]

    result = await handlers.action_pull_events(_make_integration(), _make_config())

    assert result["result"]["events_extracted"] == 1
    sent_events = sync_mocks.send.await_args.kwargs.get("events") or sync_mocks.send.await_args.args[0]
    assert sent_events[0]["event_details"]["submission_id"] == "S-GOOD"


@pytest.mark.asyncio
async def test_stale_state_entries_are_pruned(sync_mocks):
    stale_dt = (datetime.now(tz=timezone.utc) - timedelta(days=40)).isoformat()
    sync_mocks.get_state.return_value = {
        "latest_observation_at": "2026-08-09T10:00:00+00:00",
        "observations": {
            "S-STALE:sp1": {"gundi_event_id": "gid-old", "fingerprint": "x", "obs_dt": stale_dt},
        },
    }
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt="2026-08-09 11:00"),
    ]

    await handlers.action_pull_events(_make_integration(), _make_config())

    state = _saved_state(sync_mocks)
    assert "S-STALE:sp1" not in state["observations"]
    assert "S-1:sp1" in state["observations"]


@pytest.mark.asyncio
async def test_full_lookback_window_is_always_fetched(sync_mocks):
    # The old logic shrank the request window to ~1 day once state existed,
    # which hid late-submitted checklists. The full num_days must be requested.
    sync_mocks.get_state.return_value = {
        "latest_observation_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    sync_mocks.ebird.return_value = []

    await handlers.action_pull_events(_make_integration(), _make_config(num_days=5))

    params = sync_mocks.ebird.await_args.kwargs.get("params") or sync_mocks.ebird.await_args.args[2]
    assert params["back"] == 5


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

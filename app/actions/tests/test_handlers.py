from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.actions import handlers
from app.actions.configurations import PullEventsConfig, SearchParameter
from app.actions.handlers import eBirdObservation
from app.services.errors import classify_error


# Fixture timestamps are relative to a single instant captured when this module
# is imported: fixed for the whole run, so an observation built twice in one
# test fingerprints identically, but never anchored to a calendar date.
#
# They were absolute dates until this module rotted. action_pull_events prunes
# state entries older than num_days + PRUNE_MARGIN_DAYS, so once the hardcoded
# 2026-08-09 fixtures aged past that window the handler stripped every
# observation out of state before set_state, and the tests covering keyed-state
# dedup began failing with KeyError on assertions that had quietly stopped
# describing anything real.
_NOW = datetime.now(tz=timezone.utc)

# Fixture offsets, in hours before _NOW. Named so the ordering each test
# depends on (newer vs. older than a watermark) survives a careless edit.
_NEWER = 9
_RECENT = 10
_OLDER = 11
_YESTERDAY = 34
_TWO_DAYS_AGO = 58

# Every fixture must land inside the prune window that _make_config's num_days
# implies, or its test stops exercising the state assertions it was written for.
_MAX_FIXTURE_HOURS_AGO = _TWO_DAYS_AGO


def _within_prune_window(hours_ago: int) -> int:
    assert hours_ago <= _MAX_FIXTURE_HOURS_AGO, (
        f"fixture {hours_ago}h old exceeds the window this module guarantees "
        f"({_MAX_FIXTURE_HOURS_AGO}h); raise _MAX_FIXTURE_HOURS_AGO and the "
        f"num_days in _make_config together, or the handler will prune it"
    )
    return hours_ago


def _obs_dt(hours_ago: int = _RECENT) -> str:
    """An obsDt in eBird's "YYYY-MM-DD HH:MM" form, relative to _NOW."""
    return (_NOW - timedelta(hours=_within_prune_window(hours_ago))).strftime("%Y-%m-%d %H:%M")


def _obs_date_only(hours_ago: int = _RECENT) -> str:
    """An obsDt with no time portion, as eBird sends when a checklist has no start time."""
    return (_NOW - timedelta(hours=_within_prune_window(hours_ago))).strftime("%Y-%m-%d")


def _watermark(hours_ago: int) -> str:
    """A stored `latest_observation_at`, in the ISO form state round-trips through."""
    return (_NOW - timedelta(hours=_within_prune_window(hours_ago))).isoformat()


def _observation_payload(**overrides):
    payload = {
        "speciesCode": "tstbrd",
        "comName": "Test Bird",
        "sciName": "Avium testus",
        "locId": "L123",
        "locName": "Test Park",
        "obsDt": _obs_dt(),
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


_DEFAULT_NUM_DAYS = 5


def _make_config(num_days=_DEFAULT_NUM_DAYS):
    return PullEventsConfig(
        search_parameter=SearchParameter.REGION,
        region_code="US-CA",
        num_days=num_days,
    )


def test_fixtures_stay_inside_the_handlers_prune_window():
    # The invariant that keeps this module from rotting again: every fixture
    # timestamp must survive the prune step in action_pull_events, or the state
    # assertions in the tests below silently stop describing anything.
    prune_window = timedelta(days=_DEFAULT_NUM_DAYS + handlers.PRUNE_MARGIN_DAYS)
    assert timedelta(hours=_MAX_FIXTURE_HOURS_AGO) < prune_window


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
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt=_obs_dt(_RECENT)),
        _observation_payload(subId="S-1", speciesCode="sp2", obsDt=_obs_dt(_NEWER)),
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
    obs = _observation_payload(subId="S-1", speciesCode="sp1", obsDt=_obs_dt(_RECENT))
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
    original = _observation_payload(subId="S-1", speciesCode="sp1", obsDt=_obs_dt(_RECENT), howMany=3)
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
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt=_obs_dt(_RECENT)),
    ]
    await handlers.action_pull_events(_make_integration(), _make_config())
    first_state = _saved_state(sync_mocks)

    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt=_obs_dt(_RECENT)),
        _observation_payload(subId="S-2", speciesCode="sp1", obsDt=_obs_dt(_TWO_DAYS_AGO)),
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
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt=_obs_dt(_RECENT)),
    ]
    await handlers.action_pull_events(_make_integration(), _make_config())
    first_state = _saved_state(sync_mocks)

    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt=_obs_dt(_RECENT)),
        _observation_payload(subId="S-3", speciesCode="sp1", obsDt=_obs_date_only(_RECENT)),
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
    sync_mocks.get_state.return_value = {"latest_observation_at": _watermark(_RECENT)}
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-OLD", speciesCode="sp1", obsDt=_obs_dt(_OLDER)),
        _observation_payload(subId="S-NEW", speciesCode="sp1", obsDt=_obs_dt(_NEWER)),
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
        "latest_observation_at": _watermark(_RECENT),
        "observations": {},
    }
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-LATE", speciesCode="sp1", obsDt=_obs_dt(_YESTERDAY)),
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
        _observation_payload(subId="S-GOOD", speciesCode="sp1", obsDt=_obs_dt(_RECENT)),
    ]

    result = await handlers.action_pull_events(_make_integration(), _make_config())

    assert result["result"]["events_extracted"] == 1
    sent_events = sync_mocks.send.await_args.kwargs.get("events") or sync_mocks.send.await_args.args[0]
    assert sent_events[0]["event_details"]["submission_id"] == "S-GOOD"


@pytest.mark.asyncio
async def test_stale_state_entries_are_pruned(sync_mocks):
    stale_dt = (datetime.now(tz=timezone.utc) - timedelta(days=40)).isoformat()
    sync_mocks.get_state.return_value = {
        "latest_observation_at": _watermark(_RECENT),
        "observations": {
            "S-STALE:sp1": {"gundi_event_id": "gid-old", "fingerprint": "x", "obs_dt": stale_dt},
        },
    }
    sync_mocks.ebird.return_value = [
        _observation_payload(subId="S-1", speciesCode="sp1", obsDt=_obs_dt(_NEWER)),
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


# --- eBird request timeout and retry -----------------------------------------


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient, replaying a scripted list of outcomes.

    Each entry is either an exception to raise or an httpx.Response to return.
    Records the timeout it was constructed with so the test can assert it.
    """

    constructed_with = []
    script = []
    calls = 0

    def __init__(self, *args, **kwargs):
        type(self).constructed_with.append(kwargs.get("timeout"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None, headers=None):
        cls = type(self)
        outcome = cls.script[cls.calls]
        cls.calls += 1
        request = httpx.Request("GET", url)
        if isinstance(outcome, Exception):
            # httpx attaches the request to transport errors it raises.
            outcome.request = request
            raise outcome
        outcome.request = request
        return outcome


@pytest.fixture
def ebird_http(monkeypatch):
    """Patch httpx.AsyncClient inside handlers and make retry waits instant."""
    _FakeAsyncClient.constructed_with = []
    _FakeAsyncClient.script = []
    _FakeAsyncClient.calls = 0
    monkeypatch.setattr(handlers.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        handlers,
        "EBIRD_API_RETRY",
        dict(handlers.EBIRD_API_RETRY, wait_initial=0.001, wait_jitter=0.0, wait_max=0.001),
    )
    return _FakeAsyncClient


def _json_response(status_code=200, payload=None):
    return httpx.Response(status_code, json=payload if payload is not None else [])


@pytest.mark.asyncio
async def test_ebird_client_is_built_with_an_explicit_timeout(ebird_http):
    # Regression: httpx's 5s default read timeout was failing production pulls
    # on wide queries. The read budget is the one that matters.
    ebird_http.script = [_json_response(payload=[{"ok": True}])]

    await handlers._get_from_ebird("https://x/obs", "key", params={})

    timeout = ebird_http.constructed_with[0]
    assert timeout is not None, "AsyncClient must not fall back to httpx's 5s default"
    assert timeout.read == 60.0
    assert timeout.connect == 10.0


@pytest.mark.asyncio
async def test_read_timeout_is_retried_and_can_succeed(ebird_http):
    ebird_http.script = [
        httpx.ReadTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        _json_response(payload=[{"subId": "S-1"}]),
    ]

    result = await handlers._get_from_ebird("https://x/obs", "key", params={})

    assert result == [{"subId": "S-1"}]
    assert ebird_http.calls == 3


@pytest.mark.asyncio
async def test_read_timeout_gives_up_after_the_configured_attempts(ebird_http):
    ebird_http.script = [httpx.ReadTimeout("timed out")] * 10

    with pytest.raises(httpx.ReadTimeout):
        await handlers._get_from_ebird("https://x/obs", "key", params={})

    assert ebird_http.calls == handlers.EBIRD_API_RETRY["attempts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
async def test_transient_statuses_are_retried(ebird_http, status_code):
    ebird_http.script = [_json_response(status_code), _json_response(payload=[{"ok": True}])]

    result = await handlers._get_from_ebird("https://x/obs", "key", params={})

    assert result == [{"ok": True}]
    assert ebird_http.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
async def test_client_errors_are_not_retried(ebird_http, status_code):
    # A bad API key or malformed region code will not fix itself; failing fast
    # keeps a clear error in the portal instead of a delayed generic one.
    ebird_http.script = [_json_response(status_code)] * 5

    with pytest.raises(httpx.HTTPStatusError):
        await handlers._get_from_ebird("https://x/obs", "key", params={})

    assert ebird_http.calls == 1


@pytest.mark.asyncio
async def test_exhausted_transient_error_still_classifies_with_its_status(ebird_http):
    # TransientEbirdError subclasses HTTPStatusError so classify_error can
    # still read .response.status_code once the retries run out.
    ebird_http.script = [_json_response(429)] * 10

    with pytest.raises(handlers.TransientEbirdError) as exc_info:
        await handlers._get_from_ebird("https://x/obs", "key", params={})

    classified = classify_error(exc_info.value)
    assert classified.error_type == "rate_limit"
    assert classified.status_code == 429

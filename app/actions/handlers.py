import hashlib
import httpx
import json
import logging
import stamina
from datetime import datetime, timedelta, timezone
from app.actions.configurations import AuthenticateConfig, PullEventsConfig, SearchParameter
from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger
from app.services.gundi import send_events_to_gundi, update_event_in_gundi, GundiEventNotFound
from app.services.state import IntegrationStateManager
from app.services.errors import ConfigurationNotFound, ConfigurationValidationError
from app.services.utils import find_config_for_action
from gundi_core.schemas.v2 import Integration
from pydantic import BaseModel, Field, parse_obj_as, validator, ValidationError
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()

EBIRD_API = "https://api.ebird.org/v2"

# Entries older than the fetch window can never reappear in an eBird response,
# so they are pruned from state after this many extra days of margin.
PRUNE_MARGIN_DAYS = 1

# httpx defaults every phase to 5s, which eBird routinely overruns on a wide
# query (num_days up to 30, dist up to 50km): the observed production failures
# were read timeouts waiting for response headers, not connect failures. Only
# the read budget needs to be generous.
EBIRD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

# Both stops are spelled out: stamina combines `attempts` and `timeout` with
# stop_any(), so its defaults (attempts=10 / timeout=45s) would cut the curve
# below short. Waits are min(2 * 2**n + jitter, 30) for n = 0..2, i.e. 2-7,
# 4-9 and 8-13s. tenacity checks the deadline after each failed attempt, so
# the deadline has to cover the attempts before the last one at their httpx
# bound (connect + write + read = 80s each) plus the waits between them:
# 3 * 80 + 16 = 256s, hence 270 rather than the 180 this first shipped with,
# which a third read timeout exhausted, leaving the fourth attempt unreachable.
# A fully-hanging endpoint then costs up to ~350s for one call. That is inside
# MAX_ACTION_EXECUTION_TIME (540s) but not negligible, so a pull with several
# species codes can still exhaust the action budget if eBird is down outright
# -- which is the correct outcome, reported as a connectivity error.
# test_ebird_retry_deadline_leaves_every_declared_attempt_reachable pins this.
EBIRD_API_RETRY = dict(
    attempts=4,
    timeout=270.0,
    wait_initial=2.0,
    wait_jitter=5.0,
    wait_max=30.0,
)


class ObservationRecord(BaseModel):
    # gundi_event_id is None for records seeded from legacy watermark state
    # (sent before per-observation tracking existed) — those cannot be updated.
    gundi_event_id: Optional[str] = None
    fingerprint: str
    obs_dt: datetime

    @validator('obs_dt')
    def ensure_timezone_aware(cls, v):
        if v and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class State(BaseModel):
    latest_observation_at: datetime = Field(default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc))
    observations: Dict[str, ObservationRecord] = Field(default_factory=dict)

    @validator('latest_observation_at')
    def ensure_timezone_aware(cls, v):
        if v and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @property
    def is_legacy_format(self) -> bool:
        # Legacy watermark-only state never carried an 'observations' key;
        # an empty-but-present map is new-format state (e.g. fully pruned)
        # and must not be mistaken for legacy.
        return "observations" not in self.__fields_set__


class eBirdObservation(BaseModel):
    speciesCode: str
    comName: str
    sciName: str
    locId: str
    locName: str
    obsDt: datetime
    howMany: Optional[int] = None
    lat: float
    lng: float
    obsValid: bool
    obsReviewed: bool
    locationPrivate: bool
    subId: str

    @validator('obsDt', pre=True)
    def parse_date_only_obsDt(cls, v):
        # eBird omits the time portion when a checklist has no start time
        # (e.g. "2026-08-09" instead of "2026-08-09 14:32"); treat those as midnight.
        if isinstance(v, str) and len(v.strip()) == 10:
            return f"{v.strip()} 00:00"
        return v

    @validator('obsDt')
    def clean_obsDt(cls, v):
        if v and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


async def action_auth(integration:Integration, action_config: AuthenticateConfig):
    logger.info(f"Executing auth action with integration {integration} and action_config {action_config}...")

    base_url = integration.base_url or EBIRD_API

    try:
        # Use a request for region info as a proxy for verifying credentials.
        us_region_info = await get_region_info(base_url, action_config.api_key.get_secret_value(), "US")
        return {"valid_credentials": True}
    except httpx.HTTPStatusError as e:
        return {"valid_credentials": False, "status_code": e.response.status_code}


def get_auth_config(integration):
    # Look for the login credentials, needed for any action
    auth_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="auth"
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return AuthenticateConfig.parse_obj(auth_config.data)


async def get_or_create_state(integration_id: str, action_id: str):
    if saved_state := await state_manager.get_state(integration_id, action_id):
        try:
            return State.validate(saved_state)
        except ValidationError as e:
            logger.error(f"Error parsing last execution time {saved_state} state for integration ID: {integration_id}. Exception: {e}")

    return State(latest_observation_at=datetime.min.replace(tzinfo=timezone.utc))


def _observation_key(obs: "eBirdObservation") -> str:
    # A species appears at most once per checklist, so subId + speciesCode is a
    # stable natural key for an observation across fetches and edits.
    return f"{obs.subId}:{obs.speciesCode}"


def _fingerprint_event(event: dict) -> str:
    return hashlib.sha256(json.dumps(event, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@crontab_schedule("0 * * * *") # Run every hour
@activity_logger()
async def action_pull_events(integration:Integration, action_config: PullEventsConfig):

    logger.info(f"Executing 'pull_events' action with integration {integration} and action_config {action_config}...")

    auth_config = get_auth_config(integration)

    base_url = integration.base_url or EBIRD_API

    state = await get_or_create_state(str(integration.id), "pull_events")

    # Always fetch the full configured window: eBird checklists are routinely
    # submitted hours or days after the observation date, so shrinking the
    # window based on the latest delivered observation hides late submissions.
    lookback_days_to_fetch = action_config.num_days

    # Check config based on search_parameter
    if action_config.search_parameter == SearchParameter.REGION :
        if not action_config.region_code:
            raise ConfigurationValidationError("Region code is required for 'region' search parameter.")
        else:
            obs = _get_recent_observations_by_region(
                base_url, auth_config.api_key.get_secret_value(),
                lookback_days_to_fetch,
                action_config.region_code, action_config.species_code,
                action_config.include_provisional,
                species_locale=action_config.species_locale.value
            )
    else:
        if not action_config.latitude or not action_config.longitude or not action_config.distance:
            raise ConfigurationValidationError("Latitude, longitude, and distance are required for 'location' search parameter.")
        else:
            obs = _get_recent_observations_by_location(
                base_url, auth_config.api_key.get_secret_value(),
                lookback_days_to_fetch,
                action_config.latitude,
                action_config.longitude,
                action_config.distance,
                action_config.species_code,
                action_config.include_provisional,
                species_locale=action_config.species_locale.value
            )

    # Legacy state carries only the watermark: observations at or before it were
    # already delivered by the old logic, so they are seeded into the keyed map
    # without resending (with no Gundi ID, so they age out rather than update).
    is_legacy_state = state.is_legacy_format and state.latest_observation_at > datetime.min.replace(tzinfo=timezone.utc)

    new_events = []
    new_keys = []
    updates = []
    async for ob in obs:
        key = _observation_key(ob)
        event = _transform_ebird_to_gundi_event(ob)
        fingerprint = _fingerprint_event(event)
        record = state.observations.get(key)
        if record is None:
            already_sent_by_legacy_logic = is_legacy_state and ob.obsDt <= state.latest_observation_at
            state.observations[key] = ObservationRecord(fingerprint=fingerprint, obs_dt=ob.obsDt)
            if not already_sent_by_legacy_logic:
                new_events.append(event)
                new_keys.append(key)
        elif record.fingerprint != fingerprint:
            if record.gundi_event_id:
                # The record is brought up to date only once the edit is
                # delivered (or known to be undeliverable), so a failed PATCH
                # shows up as a changed fingerprint again on the next run.
                updates.append((key, record.gundi_event_id, event, fingerprint, ob.obsDt))
            else:
                record.fingerprint = fingerprint
                record.obs_dt = ob.obsDt
                logger.warning(
                    f"eBird observation {key} changed but has no Gundi event ID "
                    f"(sent before per-observation tracking); the edit will not be delivered."
                )
        state.latest_observation_at = max(state.latest_observation_at, ob.obsDt)

    events_extracted = 0
    if new_events:
        logger.info(f"Submitting {len(new_events)} eBird observations to Gundi for integration ID: {str(integration.id)}")
        response = await send_events_to_gundi(
            events=new_events,
            integration_id=str(integration.id)
        )
        if isinstance(response, list) and len(response) == len(new_events):
            for key, trace in zip(new_keys, response):
                object_id = trace.get("object_id") if isinstance(trace, dict) else None
                state.observations[key].gundi_event_id = str(object_id) if object_id else None
        else:
            logger.warning(
                f"Unexpected response shape from send_events_to_gundi; "
                f"updates will be unavailable for this batch of {len(new_events)} events."
            )
        events_extracted = len(new_events)
        # Checkpoint: the runner cancels the handler at MAX_ACTION_EXECUTION_TIME
        # and cancellation bypasses every `except Exception` below, so the ids
        # just recorded must be durable before the update loop can spend that
        # budget -- or the next run re-sends these events as new.
        await _save_state(integration, action_config, state)

    events_updated = 0
    update_failures = []
    for key, event_id, event, fingerprint, obs_dt in updates:
        record = state.observations[key]
        logger.info(f"Updating previously sent event {event_id} in Gundi for integration ID: {str(integration.id)}")
        try:
            await update_event_in_gundi(
                event_id=event_id,
                event=event,
                integration_id=str(integration.id)
            )
        except Exception as e:
            if isinstance(e, GundiEventNotFound):
                # The PATCH itself answered 404: the event was deleted in Gundi
                # and will not come back (the helper does not retry it either).
                # Drop the id so this record stops PATCHing a ghost on every
                # edit, and acknowledge the edit as undeliverable, like a
                # legacy record. Any other 404 (the portal not knowing the
                # integration, say) takes the branch below and keeps the id.
                logger.warning(
                    f"Gundi event {event_id} for eBird observation {key} no longer exists; "
                    f"further edits to it will not be delivered."
                )
                record.gundi_event_id = None
            else:
                # Leave the record untouched so the next run sees the change
                # again and retries; keep going so the other edits, and the
                # state save below, are not lost to one failure.
                logger.error(f"Failed to update Gundi event {event_id} for eBird observation {key}: {e}")
                update_failures.append(e)
                continue
        else:
            events_updated += 1
        record.fingerprint = fingerprint
        record.obs_dt = obs_dt
        # Checkpoint each delivered (or written-off) edit for the same reason:
        # a cancellation mid-batch must not undo the PATCHes that landed.
        await _save_state(integration, action_config, state)

    await _save_state(integration, action_config, state)

    if update_failures:
        # Reported only now: state is saved, so the events POSTed this run are
        # recorded (no duplicates next run) and the failed edits stay retryable.
        raise update_failures[0]

    return {'result': {'events_extracted': events_extracted, 'events_updated': events_updated}}


async def _save_state(integration: Integration, action_config: PullEventsConfig, state: State) -> None:
    # Entries older than the fetch window cannot reappear in a response, so
    # dropping them keeps state size bounded.
    prune_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=action_config.num_days + PRUNE_MARGIN_DAYS)
    state.observations = {key: record for key, record in state.observations.items() if record.obs_dt >= prune_cutoff}

    await state_manager.set_state(
        str(integration.id),
        "pull_events",
        json.loads(state.json())
    )


class TransientEbirdError(httpx.HTTPStatusError):
    """A 429 or 5xx from eBird: worth retrying, unlike a 4xx.

    Subclasses HTTPStatusError so it keeps carrying `.response`, which
    `app.services.errors.classify_error` reads to report a rate limit or a
    provider fault rather than a generic failure when the retries run out.
    """


async def _get_from_ebird(url: str, api_key: str, params: dict):
    headers = {
        "X-eBirdApiToken": api_key
    }

    payload = None
    # httpx.TransportError covers the read timeout these retries exist for, plus
    # connect and protocol failures. A 4xx raises plain HTTPStatusError and is
    # not retried: a bad API key or a malformed region code will not fix itself.
    # retry_context (rather than the @stamina.retry decorator) reads the policy
    # at call time, so tests can shorten the waits -- and it matches the idiom
    # already used in app/services/webhooks.py.
    async for attempt in stamina.retry_context(
        on=(httpx.TransportError, TransientEbirdError), **EBIRD_API_RETRY
    ):
        with attempt:
            async with httpx.AsyncClient(timeout=EBIRD_TIMEOUT) as client:
                r = await client.get(url, params=params, headers = headers)
                if r.status_code == 429 or r.status_code >= 500:
                    raise TransientEbirdError(
                        f"eBird returned {r.status_code} for {r.request.url}",
                        request=r.request,
                        response=r,
                    )
                r.raise_for_status()
                payload = r.json()
    return payload

async def _get_recent_observations_by_region(base_url: str, api_key: str, num_days: int, region_code: str, 
                                             species_code: str = None, include_provisional: bool = False,
                                             species_locale: str = None):

        params = {
             "back": num_days,
             "includeProvisional": include_provisional,
            "sppLocale": species_locale
        }
        url = f"{base_url}/data/obs/{region_code}/recent"
        logger.info(f"Loading eBird observations for last {num_days} days near region code {region_code}.")

        async for item in _get_recent_observations(url, api_key, params, species_code):
            yield item


async def _get_recent_observations_by_location(base_url: str, api_key: str, num_days: int, lat: float, 
                                               lng: float, dist: float, species_code: str = None,
                                               include_provisional: bool = False, species_locale: str = None):

        # lat/lng belong in params, not baked into the URL. _get_recent_observations
        # appends /{speciesCode} to whatever it is handed, so a URL carrying its own
        # query string produced ".../geo/recent?lat=1.5&lng=2.5/amecro" -- the species
        # code landing inside the lng value. eBird's endpoint is
        # /data/obs/geo/recent/{speciesCode} with lat and lng as query parameters.
        #
        # Keeping them here also survives the httpx upgrade: 0.24 merges `params`
        # into a URL's existing query string, but 0.28 replaces it outright, which
        # would silently drop lat and lng from every geo request.
        params = {
            "lat": lat,
            "lng": lng,
            "dist": dist,
            "back": num_days,
            "includeProvisional": include_provisional,
            "sppLocale": species_locale
        }
        url = f"{base_url}/data/obs/geo/recent"

        logger.info(f"Loading eBird observations for last {num_days} days near ({lat}, {lng}).")
        async for item in _get_recent_observations(url, api_key, params, species_code):
            yield item


async def _get_recent_observations(base_url, api_key, params, species_code: str = None):

        if(species_code):
            species = species_code.split(",")
            for specie in species:
                url = f"{base_url}/{specie}"
                obs = await _get_from_ebird(url, api_key, params=params)
                if obs:
                    logger.info(f"Loading observations for specie '{specie}'.")
                    for ob in obs:
                        parsed = _parse_observation(ob)
                        if parsed:
                            yield parsed
                else:
                    logger.info(f"No observations found for specie '{specie}'.")

        else:
            obs = await _get_from_ebird(base_url, api_key, params=params)
            for ob in obs:
                parsed = _parse_observation(ob)
                if parsed:
                    yield parsed


def _parse_observation(ob: dict) -> Optional[eBirdObservation]:
    # One malformed record must not abort the whole pull and lose the valid
    # observations fetched alongside it.
    try:
        return parse_obj_as(eBirdObservation, ob)
    except ValidationError as e:
        record_ref = f"subId={ob.get('subId')} speciesCode={ob.get('speciesCode')} obsDt={ob.get('obsDt')}" if isinstance(ob, dict) else repr(type(ob))
        logger.warning(f"Skipping malformed eBird observation record ({record_ref}): {e}")
        return None


async def get_region_info(base_url: str, api_key: str, region_code: str):
    url = f"{base_url}/ref/region/info/{region_code}"
    return await _get_from_ebird(url, api_key, params=None)


def _transform_ebird_to_gundi_event(obs: eBirdObservation):
    
    return {
        "title": f"{obs.comName} observation",
        "event_type": "ebird_observation",
        "recorded_at": obs.obsDt.isoformat(),
        "location": {
            "lat": obs.lat,
            "lon": obs.lng
        },
        "event_details": {
            "common_name": obs.comName,
            "scientific_name": obs.sciName,
            "species_code": obs.speciesCode,
            "location_id": obs.locId,
            "location_name": obs.locName,
            "location_private": obs.locationPrivate,
            "quantity": obs.howMany,
            "valid": obs.obsValid,
            "reviewed": obs.obsReviewed,
            "submission_id": obs.subId,
            "attribution": "Data from https://eBird.org, Cornell Lab of Ornithology."
        }
    }
import httpx
import logging
import math
from datetime import datetime, timezone
from app.actions.configurations import AuthenticateConfig, PullEventsConfig, SearchParameter
from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger
from app.services.gundi import send_events_to_gundi
from app.services.state import IntegrationStateManager
from app.services.errors import ConfigurationNotFound, ConfigurationValidationError
from app.services.utils import find_config_for_action
from gundi_core.schemas.v2 import Integration
from pydantic import BaseModel, parse_obj_as, validator, ValidationError
from typing import List, Optional

logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()

EBIRD_API = "https://api.ebird.org/v2"
SECONDS_IN_DAY = 86400 # 24 hours * 60 minutes * 60 seconds


class LatestObservationDatetimeState(BaseModel):
    latest_observation_datetime: datetime

    @validator('latest_observation_datetime', pre=True, always=True)
    def clean_latest_observation_datetime(cls, v):
        parsed = datetime.fromisoformat(v)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


class LatestExecutionDatetimeState(BaseModel):
    latest_execution_time: datetime

    @validator('latest_execution_time', pre=True, always=True)
    def clean_latest_execution_time(cls, v):
        parsed = datetime.fromisoformat(v)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


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

    @validator('obsDt', pre=True, always=True)
    def clean_obsDt(cls, v):
        # Parse the datetime string coming from eBird and return it in ISO format
        parsed = datetime.fromisoformat(v)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        v = parsed.isoformat()

        return v

async def handle_transformed_data(transformed_data, integration_id, action_id):
    try:
        response = await send_events_to_gundi(
            events=transformed_data,
            integration_id=integration_id
        )
    except httpx.HTTPError as e:
        msg = f'Sensors API returned error for integration_id: {integration_id}. Exception: {e}'
        logger.exception(
            msg,
            extra={
                'needs_attention': True,
                'integration_id': integration_id,
                'action_id': action_id
            }
        )
        return [msg]
    else:
        return response



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

async def filter_ebird_events(integration_id: str, events: List[dict]) -> List[dict]:
    saved_state = await state_manager.get_state(
        integration_id,
        "pull_events",
        "latest_observation_datetime"
    )
    if saved_state:
        try:
            state = LatestObservationDatetimeState.parse_obj(saved_state)
        except ValidationError as e:
            logger.error(f"Error parsing latest_observation_datetime state for integration ID: {integration_id}. Exception: {e}")
            return events
        latest_observation_datetime = state.latest_observation_datetime
        filtered_events = [
            event for event in events
            if event["recorded_at"] > latest_observation_datetime
        ]
        logger.info(f"Filtered {len(events) - len(filtered_events)} eBird observations older than latest recorded observation datetime {latest_observation_datetime} for integration ID: {integration_id}")
        return filtered_events
    else:
        return events

@crontab_schedule("0 * * * *") # Run every hour
@activity_logger()
async def action_pull_events(integration:Integration, action_config: PullEventsConfig):

    logger.info(f"Executing 'pull_events' action with integration {integration} and action_config {action_config}...")

    auth_config = get_auth_config(integration)

    base_url = integration.base_url or EBIRD_API

    # Check if latest_execution_time exists in state
    saved_latest_execution_time = await state_manager.get_state(
        str(integration.id),
        "pull_events",
        "latest_execution_time"
    )
    if saved_latest_execution_time:
        try:
            state = LatestExecutionDatetimeState.parse_obj(saved_latest_execution_time)
        except ValidationError as e:
            logger.error(f"Error parsing latest_execution_time state for integration ID: {str(integration.id)}. Exception: {e}")
            lookback_days_to_fetch = action_config.num_days
        else:
            latest_execution_time = state.latest_execution_time

            logger.info(f"Latest execution time found in state: {latest_execution_time.isoformat()} for integration ID: {str(integration.id)}")

            # If it exists, adjust num_days to cover from that time to now
            now = datetime.now(tz=timezone.utc)
            delta = now - latest_execution_time
            days_difference = math.ceil(delta.total_seconds() / SECONDS_IN_DAY)
            lookback_days_to_fetch = max(1, days_difference)
            logger.info(f"Adjusted num_days to {lookback_days_to_fetch} to cover from latest execution time to now for integration ID: {str(integration.id)}")
    else:
        lookback_days_to_fetch = action_config.num_days
        logger.info(f"No latest execution time found in state. Using configured num_days: {lookback_days_to_fetch}.")


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

    transformed_events = []
    events_extracted = 0
    async for ob in obs:
        transformed_events.append(_transform_ebird_to_gundi_event(ob))
    
    if transformed_events:
        filtered_events = await filter_ebird_events(str(integration.id), transformed_events)
        if filtered_events:
            logger.info(f"Submitting {len(filtered_events)} eBird observations to Gundi for integration ID: {str(integration.id)}")
            response = await handle_transformed_data(
                transformed_data=filtered_events,
                integration_id=str(integration.id),
                action_id="pull_events"
            )
            # check for error in response
            if "error" in response:
                logger.error(f"Error submitting eBird observations to Gundi for integration ID: {str(integration.id)}. Response: {response}")
                events_extracted = 0
            else:
                latest_time = max(filtered_events, key=lambda obs: obs["recorded_at"])["recorded_at"]
                state = {"latest_observation_datetime": latest_time.isoformat()}
                await state_manager.set_state(
                    str(integration.id),
                    "pull_events",
                    state,
                    "latest_observation_datetime"
                )
                events_extracted += len(filtered_events)
        else:
            logger.info(f"No new eBird observations to submit to Gundi after filtering for integration ID: {str(integration.id)}")
    else:
        logger.info(f"No eBird observations to submit to Gundi for integration ID: {str(integration.id)}")

    # Save latest_execution_time to state
    state = {"latest_execution_time": datetime.now(tz=timezone.utc).isoformat()}
    await state_manager.set_state(
        str(integration.id),
        "pull_events",
        state,
        "latest_execution_time"
    )
    return {'result': {'events_extracted': events_extracted}}

async def _get_from_ebird(url: str, api_key: str, params: dict):
    headers = {
        "X-eBirdApiToken": api_key
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, headers = headers)
        r.raise_for_status()
        return r.json()

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

        params = {
            "dist": dist,
            "back": num_days,
            "includeProvisional": include_provisional,
            "sppLocale": species_locale
        }
        url = f"{base_url}/data/obs/geo/recent?lat={lat}&lng={lng}"

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
                        yield parse_obj_as(eBirdObservation, ob)
                else:
                    logger.info(f"No observations found for specie '{specie}'.")
        
        else:
            obs = await _get_from_ebird(base_url, api_key, params=params)
            for ob in obs:
                yield parse_obj_as(eBirdObservation, ob)


async def get_region_info(base_url: str, api_key: str, region_code: str):
    url = f"{base_url}/ref/region/info/{region_code}"
    return await _get_from_ebird(url, api_key, params=None)


def _transform_ebird_to_gundi_event(obs: eBirdObservation):
    
    return {
        "title": f"{obs.comName} observation",
        "event_type": "ebird_observation",
        "recorded_at": obs.obsDt,
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
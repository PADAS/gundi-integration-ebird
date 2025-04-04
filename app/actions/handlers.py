import httpx
import logging
from datetime import datetime
from app.actions.configurations import AuthenticateConfig, PullEventsConfig, SearchParameter
from app.services.activity_logger import activity_logger
from app.services.gundi import send_events_to_gundi
from app.services.state import IntegrationStateManager
from app.services.errors import ConfigurationNotFound, ConfigurationValidationError
from app.services.utils import find_config_for_action
from gundi_core.schemas.v2 import Integration
from pydantic import BaseModel, parse_obj_as, validator
from typing import List, Optional

logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()

EBIRD_API = "https://api.ebird.org/v2"
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

@activity_logger()
async def action_pull_events(integration:Integration, action_config: PullEventsConfig):

    logger.info(f"Executing 'pull_events' action with integration {integration} and action_config {action_config}...")

    auth_config = get_auth_config(integration)

    base_url = integration.base_url or EBIRD_API

    # Check config based on search_parameter
    if action_config.search_parameter == SearchParameter.REGION :
        if not action_config.region_code:
            raise ConfigurationValidationError("Region code is required for 'region' search parameter.")
        else:
            obs = _get_recent_observations_by_region(
                base_url, auth_config.api_key.get_secret_value(),
                action_config.num_days,
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
                action_config.num_days,
                action_config.latitude,
                action_config.longitude,
                action_config.distance,
                action_config.species_code,
                action_config.include_provisional,
                species_locale=action_config.species_locale.value
            )

    to_send = []
    async for ob in obs:
        to_send.append(_transform_ebird_to_gundi_event(ob))
    
    if to_send:
        logger.info(f"Submitting {len(to_send)} eBird observations to Gundi for integration ID: {str(integration.id)}")
        await send_events_to_gundi(
            events=to_send,
            integration_id=str(integration.id)
        )
    else:
        logger.info(f"No eBird observations to submit to Gundi for integration ID: {str(integration.id)}")

    return {'result': {'events_extracted': len(to_send)}}

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
from typing import Optional
from enum import Enum
import pydantic
from .core import PullActionConfiguration, AuthActionConfiguration, ExecutableActionMixin
from app.services.utils import FieldWithUIOptions, GlobalUISchemaOptions, UIOptions


class SearchParameter(Enum):
    REGION = "region"
    LAT_LON_DISTANCE = "lat-lon-distance"


class SpeciesLocale(Enum):
    EN = "en"
    ES = "es"
    FR = "fr"
    PT = "pt_PT"
    DE = "de"


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    api_key: pydantic.SecretStr = pydantic.Field(..., title = "eBird API Key", 
                                  description = "API key generated from eBird's website at https://ebird.org/api/keygen",
                                  format="password")
    
class PullEventsConfig(PullActionConfiguration):

    search_parameter: SearchParameter = pydantic.Field(
        SearchParameter.LAT_LON_DISTANCE,
        title="Search Parameter",
        description="A parameter the integration will use for fetching events."
    )
    latitude: float = pydantic.Field(
        None,
        title="Latitude",
        description="Latitude of point to search around. If not present, a search region should be included instead.",
        ge=-90.0,
        le=90.0
    )
    longitude: float = pydantic.Field(
        None,
        title="Longitude",
        description="Longitude of point to search around. If not present, a search region should be included instead.",
        ge=-180.0,
        le=360.0
    )
    distance: float = FieldWithUIOptions(
        None,
        title="Distance",
        description="Distance in kilometers to search around.  Max: 50km.  Default: 25km.",
        ge=1,
        le=50,
        ui_options=UIOptions(
            widget="range",  # This will be rendered ad a range slider
        )
    )
    
    num_days: int = FieldWithUIOptions(
        2,
        title="Number of Days",
        description = "Number of days of data to pull from eBird.  Default: 2. Min: 1, Max: 30.",
        ge=1,
        le=30,
        ui_options = UIOptions(
            widget="range",  # This will be rendered ad a range slider
        )
    )

    region_code: Optional[str] = pydantic.Field(None, title="Region Code",
        description="An eBird region code that should be used in the query.  Either a region code or a combination of latitude, longitude and distance should be included.")
    
    species_code: Optional[str] = pydantic.Field(None, title="Species Code",
        description="An eBird species code that should be used in the query.  If not included, all species will be searched.")

    include_provisional: bool = pydantic.Field(False, title="Include Unreviewed", 
        description="Whether or not to include observations that have not yet been reviewed.  Default: False.")
    species_locale: SpeciesLocale = pydantic.Field(
        SpeciesLocale.EN,
        title="Species Locale",
        description="Language to use for species information. Default: EN (English)."
    )
    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "num_days",
            "species_code",
            "species_locale",
            "search_parameter",
            "distance",
            "latitude",
            "longitude",
            "region_code",
            "include_provisional"
        ],
    )
    
    # Temporary validator to cope with a limitation in Gundi Portal.
    @pydantic.validator("region_code", "species_code", always=True)
    def validate_region_code(cls, v, values):
        if 'any' == str(v).lower():
            return None
        return v

    class Config:
        @staticmethod
        def schema_extra(schema: dict):
            # Remove latitude, longitude, distance and region_code from the root properties
            schema["properties"].pop("latitude", None)
            schema["properties"].pop("longitude", None)
            schema["properties"].pop("distance", None)
            schema["properties"].pop("region_code", None)

            # Show region_code OR latitude & longitude & distance based on search_parameter
            schema.update({
                "if": {
                    "properties": {
                        "search_parameter": {"const": "lat-lon-distance"}
                    }
                },
                "then": {
                    "required": ["latitude", "longitude", "distance"],
                    "properties": {
                        "latitude": {
                            "type": "number",
                            "title": "Latitude",
                            "description": "Latitude of point to search around.",
                            "minimum": -90.0,
                            "maximum": 90.0
                        },
                        "longitude": {
                            "type": "number",
                            "title": "Longitude",
                            "description": "Longitude of point to search around.",
                            "minimum": -180.0,
                            "maximum": 360.0
                        },
                        "distance": {
                            "type": "number",
                            "title": "Distance",
                            "maximum": 50,
                            "minimum": 1,
                            "description": "Distance in kilometers to search around.  Max: 50km."
                        },
                    }
                },
                "else": {
                    "required": ["region_code"],
                    "properties": {
                        "region_code": {
                            "type": "string",
                            "title": "Region Code",
                            "description": "An eBird region code that should be used in the query."
                        }
                    }
                }
            })

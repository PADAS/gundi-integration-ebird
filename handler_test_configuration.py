import asyncio

# ---------- remove this ------------
from gundi_core.schemas.v2 import (
    Integration,
    IntegrationAction,
    IntegrationType,
    UUID,
    IntegrationActionConfiguration,
    IntegrationActionSummary,
    ConnectionRoute,
    Organization,
)
from app.actions.handlers import action_pull_events
from app.actions.configurations import PullEventsConfig

if __name__ == "__main__":
    action_config = PullEventsConfig(some_important_value=42.0)

    integration = Integration(
        id=UUID("e9c1eef0-7c28-46bb-8155-fe9b31dedce7"),
        name="Test eBird Connection",
        type=IntegrationType(
            id=UUID("cd401782-cf42-4c38-90c9-8248536139af"),
            name="eBird",
            value="ebird",
            description="A type for eBird connections",
            actions=[
                IntegrationAction(
                    id=UUID("e0d2b2de-a277-4f67-89ef-13ef0e07623d"),
                    type="auth",
                    name="Auth",
                    value="auth",
                    description="",
                    action_schema={
                        "type": "object",
                        "title": "AuthenticateConfig",
                        "required": ["username", "password"],
                        "properties": {
                            "email": {"type": "string", "title": "Email"},
                            "password": {"type": "string", "title": "Password", "format": "password"},
                        },
                    },
                ),
                IntegrationAction(
                    id=UUID("1306da74-7e87-45a0-a5de-c11974e4e63e"),
                    type="pull",
                    name="Pull Events",
                    value="pull_events",
                    description="eBird pull events action",
                    action_schema={
                        "type": "object",
                        "title": "PullEventsConfig",
                        "required": ["some_important_value"],
                        "properties": {
                            "some_important_value": {
                                "type": "number",
                                "title": "Some important value",
                                "default": 0.0,
                                "maximum": 50.0,
                                "minimum": 0.0,
                                "description": "Some value of great importance.",
                            },
                        },
                    },
                ),
            ],
            webhook=None,
        ),
        base_url="https://api.ebird.org",
        enabled=True,
        owner=Organization(
            id=UUID("b56b585d-7f94-4a45-b8af-bb7dc6a9c731"),
            name="EarthRanger Developers",
            description="",
        ),
        configurations=[
            IntegrationActionConfiguration(
                id=UUID("7c0cbf42-ad62-4725-8380-e4cf29acc406"),
                integration=UUID("e9c1eef0-7c28-46bb-8155-fe9b31dedce7"),
                action=IntegrationActionSummary(
                    id=UUID("1306da74-7e87-45a0-a5de-c11974e4e63e"),
                    type="pull",
                    name="Pull Events",
                    value="pull_events",
                ),
                data={
                    "some_important_value": 7.0,
                },
            ),
            IntegrationActionConfiguration(
                id=UUID("91930701-0cf3-4201-a4a5-02b458c460e1"),
                integration=UUID("e9c1eef0-7c28-46bb-8155-fe9b31dedce7"),
                action=IntegrationActionSummary(
                    id=UUID("e0d2b2de-a277-4f67-89ef-13ef0e07623d"),
                    type="auth",
                    name="Auth",
                    value="auth",
                ),
                data={
                    "username": "integrations@earthranger.com",
                    "password": "something-fancy"
                },
            ),
        ],
    )
    asyncio.run(action_pull_events(integration=integration, action_config=action_config))

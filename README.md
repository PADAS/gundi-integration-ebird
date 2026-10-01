# gundi-integration-ebird
Gundi integration for eBird, from CornellLab of Ornithology.

## Usage
TBD

## Activity-log redaction
The configuration attached to every activity-log event is redacted before publishing (`app/services/redaction.py`): values under secret-looking keys (`password`, `token`, `api_key`, `secret`, ...) and fields your config model declares as `SecretStr`, `Field(format="password")` or `UIOptions(widget="password")` are replaced with `**********`, matched by field name or alias and at any depth of nested models. Declare secrets that way and they never reach the portal's activity log in clear, whatever their name.

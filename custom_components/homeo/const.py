"""Constants for the Homeo integration."""

DOMAIN = "homeo"
RULES_DIR = "homeo"  # <config>/homeo/*.homeo
RULES_SUFFIX = ".homeo"
FILE_DEBOUNCE = 0.5  # seconds without further file events before reloading
ENTITY_DEBOUNCE = 2.0  # seconds after an entity appears before retrying files with errors
BOOT_TIME_KEY = "homeo_boot_time"  # hass.data: when homeo first started in this HA run
EVENT_RULE_FIRED = "homeo_rule_fired"
CONF_DRY_RUN = "dry_run"
MACHINE_UNIQUE_ID_PREFIX = "homeo_state_"
ISSUE_PREFIX = "compile_error_"

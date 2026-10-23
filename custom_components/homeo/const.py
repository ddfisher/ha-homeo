"""Constants for the Homeo integration."""

from datetime import timedelta

DOMAIN = "homeo"
RULES_DIR = "homeo"  # <config>/homeo/*.homeo
RULES_SUFFIX = ".homeo"
POLL_INTERVAL = timedelta(seconds=2)
EVENT_RULE_FIRED = "homeo_rule_fired"
CONF_DRY_RUN = "dry_run"
MACHINE_UNIQUE_ID_PREFIX = "homeo_state_"
ISSUE_PREFIX = "compile_error_"

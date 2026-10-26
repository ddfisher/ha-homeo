"""Websocket API for the editor panel (§19): list, read, check, save and delete rule files,
explain rules, and look up values for hovers.

All commands need an admin user: rule files drive devices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, RULES_SUFFIX, SIGNAL_FILES_CHANGED
from .core.errors import Diagnostic
from .core.explain import NotFound, explain, value
from .manager import file_version

if TYPE_CHECKING:
    from .manager import HomeoManager

_REGISTERED = f"{DOMAIN}_websocket_registered"


def _file_name(value: Any) -> str:
    """Any name the integration loads (`<config>/homeo/*.homeo`, so `küche.homeo` or
    `living room.homeo` too), as long as it stays in that folder and isn't hidden."""
    if (
        not isinstance(value, str)
        or not value.endswith(RULES_SUFFIX)
        or value == RULES_SUFFIX
        or value.startswith(".")
        or any(c in value for c in "/\\\0")
    ):
        raise vol.Invalid(f"a file name in the rules folder, like `bathroom{RULES_SUFFIX}`")
    return value


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the commands (once per HA run: commands can't be unregistered)."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    for command in (
        ws_files,
        ws_subscribe_files,
        ws_read,
        ws_check,
        ws_save,
        ws_delete,
        ws_explain,
        ws_value,
    ):
        websocket_api.async_register_command(hass, command)


def _manager(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> HomeoManager | None:
    entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if not entries:
        connection.send_error(msg["id"], "not_loaded", "Homeo isn't set up")
        return None
    return entries[0].runtime_data


def diagnostic_json(d: Diagnostic) -> dict[str, Any]:
    return {
        "line": d.span.line,
        "col": d.span.col,
        "end_line": d.span.end_line,
        "end_col": d.span.end_col,
        "severity": d.severity,
        "message": d.message,
        "hint": d.hint,
    }


@websocket_api.websocket_command({vol.Required("type"): "homeo/files"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_files(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The rule files, whether each is running, and how many errors it has."""
    if (manager := _manager(hass, connection, msg)) is None:
        return
    files = await manager.async_list_files()
    connection.send_result(msg["id"], {"files": files, "directory": str(manager.directory)})


@websocket_api.websocket_command({vol.Required("type"): "homeo/files/subscribe"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_subscribe_files(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Like homeo/files, sent now and again after every reload (a save here or elsewhere, or
    a change the file watcher saw), so the panel stays current and notices outside edits."""
    if (manager := _manager(hass, connection, msg)) is None:
        return

    async def send() -> None:
        files = await manager.async_list_files()
        connection.send_message(
            websocket_api.event_message(
                msg["id"], {"files": files, "directory": str(manager.directory)}
            )
        )

    @callback
    def changed() -> None:
        hass.async_create_task(send(), "homeo files changed")

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, SIGNAL_FILES_CHANGED, changed
    )
    connection.send_result(msg["id"])
    await send()


@websocket_api.websocket_command(
    {vol.Required("type"): "homeo/file/read", vol.Required("name"): _file_name}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_read(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    text = await manager.async_read_file(msg["name"])
    if text is None:
        connection.send_error(msg["id"], "not_found", f"{msg['name']} doesn't exist")
        return
    connection.send_result(
        msg["id"], {"name": msg["name"], "text": text, "version": file_version(text)}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "homeo/file/check",
        vol.Required("name"): _file_name,
        vol.Required("text"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_check(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Compile the text without loading it, for inline errors while editing."""
    if (manager := _manager(hass, connection, msg)) is None:
        return
    diagnostics = manager.check_text(msg["name"], msg["text"])
    connection.send_result(msg["id"], {"diagnostics": [diagnostic_json(d) for d in diagnostics]})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "homeo/file/save",
        vol.Required("name"): _file_name,
        vol.Required("text"): str,
        # The version the edit started from (None: a new file). If the file has changed since
        # (another editor, a git pull), the save is refused with `conflict` unless `force`.
        vol.Optional("version"): vol.Any(str, None),
        vol.Optional("force", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Write the file and reload. A file with errors is saved too; its previous version keeps
    running (§2), and the result says so."""
    if (manager := _manager(hass, connection, msg)) is None:
        return
    name = msg["name"]
    if "version" in msg and not msg["force"]:
        current = file_version(await manager.async_read_file(name))
        if current != msg["version"]:
            what = "was deleted" if current is None else "was changed outside the editor"
            if msg["version"] is None and current is not None:
                what = "was created outside the editor"
            connection.send_error(msg["id"], "conflict", f"{name} {what} since you opened it")
            return
    await manager.async_save_file(name, msg["text"])
    errors = manager.errors.get(name, [])
    connection.send_result(
        msg["id"],
        {
            "loaded": name in manager.programs and not errors,
            "diagnostics": [diagnostic_json(d) for d in errors],
            "version": file_version(msg["text"]),
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "homeo/file/delete", vol.Required("name"): _file_name}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    if (manager := _manager(hass, connection, msg)) is None:
        return
    await manager.async_delete_file(msg["name"])
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {vol.Required("type"): "homeo/explain", vol.Required("target"): str}
)
@websocket_api.require_admin
@callback
def ws_explain(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Like the `homeo.explain` action, for the panel."""
    if (manager := _manager(hass, connection, msg)) is None:
        return
    try:
        result = explain(manager.runtime, msg["target"])
    except NotFound as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    connection.send_result(msg["id"], {"explanations": result})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "homeo/value",
        vol.Required("name"): _file_name,
        vol.Required("symbol"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_value(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The current value of a `let` or `state` name in a file's running version, for the
    editor's hover (lighter than homeo/explain, and limited to that file's names)."""
    if (manager := _manager(hass, connection, msg)) is None:
        return
    try:
        result = value(manager.runtime, msg["name"], msg["symbol"])
    except NotFound as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    connection.send_result(msg["id"], result)

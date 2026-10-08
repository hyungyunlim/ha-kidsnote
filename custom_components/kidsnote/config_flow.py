"""Config flow: Kidsnote login, then optional Immich target and delivery script."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiohttp import ClientError, DummyCookieJar
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .const import (
    CONF_ALBUM,
    CONF_IMMICH_API_KEY,
    CONF_IMMICH_URL,
    CONF_INTERVAL,
    CONF_SCRIPT,
    DEFAULT_ALBUM,
    DEFAULT_INTERVAL,
    DOMAIN,
)
from .immich import Immich, ImmichError
from .kidsnote import Kidsnote, KidsnoteAuthError, KidsnoteError

PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
LOGIN_SCHEMA = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): PASSWORD})
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_IMMICH_URL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
        vol.Optional(CONF_IMMICH_API_KEY): PASSWORD,
        vol.Optional(CONF_SCRIPT): selector.EntitySelector(selector.EntitySelectorConfig(domain="script")),
        vol.Required(CONF_INTERVAL, default=DEFAULT_INTERVAL): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=15, max=1440, step=5, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
)


def _album_schema(albums: list[dict[str, Any]]) -> vol.Schema:
    """Existing albums are stored by id; typing a name template is still allowed."""
    options = [selector.SelectOptionDict(value=DEFAULT_ALBUM, label=DEFAULT_ALBUM)] + [
        selector.SelectOptionDict(value=a["id"], label=f"{a['albumName']} ({a.get('assetCount', 0)})")
        for a in sorted(albums, key=lambda a: str(a.get("albumName", "")).lower())
    ]
    return vol.Schema(
        {
            vol.Required(CONF_ALBUM, default=DEFAULT_ALBUM): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
        }
    )


async def _check_kidsnote(hass: HomeAssistant, username: str, password: str) -> str | None:
    """Error key, or None when the login works and the account has children."""
    # ponytail: HA closes it at shutdown (it warns if we close it); flows are rare.
    session = async_create_clientsession(hass, cookie_jar=DummyCookieJar())
    try:
        children = await Kidsnote(session, username, password).children()
    except KidsnoteAuthError as err:
        return err.reason
    except (KidsnoteError, ClientError, TimeoutError):
        return "cannot_connect"
    return None if children else "no_children"


async def _immich_albums(hass: HomeAssistant, options: Mapping[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """(errors, albums). Both are empty when no Immich URL is set."""
    if not options.get(CONF_IMMICH_URL):
        return {}, []
    if not options.get(CONF_IMMICH_API_KEY):
        return {CONF_IMMICH_API_KEY: "immich_key_required"}, []
    try:
        immich = Immich(async_get_clientsession(hass), options[CONF_IMMICH_URL], options[CONF_IMMICH_API_KEY])
        return {}, await immich.albums()
    except ImmichError as err:
        return {"base": "immich_auth" if err.status in (401, 403) else "immich_connect"}, []


class KidsnoteConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._albums: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            options = {k: v for k, v in user_input.items() if k not in (CONF_USERNAME, CONF_PASSWORD)}
            if reason := await _check_kidsnote(self.hass, username, user_input[CONF_PASSWORD]):
                errors["base"] = reason
            else:
                errors, self._albums = await _immich_albums(self.hass, options)
            if not errors:
                self._data = {CONF_USERNAME: username, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                self._options = options
                if options.get(CONF_IMMICH_URL):
                    return await self.async_step_album()
                return self.async_create_entry(title=username, data=self._data, options=options)
        schema = LOGIN_SCHEMA.extend(OPTIONS_SCHEMA.schema)
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input or {}), errors=errors
        )

    async def async_step_album(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._data[CONF_USERNAME], data=self._data, options={**self._options, **user_input}
            )
        return self.async_show_form(step_id="album", data_schema=_album_schema(self._albums))

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if reason := await _check_kidsnote(self.hass, entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]):
                errors["base"] = reason
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD}),
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KidsnoteOptionsFlow:
        return KidsnoteOptionsFlow()


class KidsnoteOptionsFlow(OptionsFlowWithReload):
    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._albums: list[dict[str, Any]] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, self._albums = await _immich_albums(self.hass, user_input)
            if not errors:
                if user_input.get(CONF_IMMICH_URL):
                    self._options = user_input
                    return await self.async_step_album()
                return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, user_input or self.config_entry.options),
            errors=errors,
        )

    async def async_step_album(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self._options, **user_input})
        schema = self.add_suggested_values_to_schema(_album_schema(self._albums), self.config_entry.options)
        return self.async_show_form(step_id="album", data_schema=schema)

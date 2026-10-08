"""Back up Kidsnote reports and albums to a local folder, Immich and a delivery script."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import time

from aiohttp import ClientError, DummyCookieJar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALBUM,
    CONF_IMMICH_API_KEY,
    CONF_IMMICH_URL,
    CONF_INTERVAL,
    CONF_SCRIPT,
    DEFAULT_ALBUM,
    DEFAULT_INTERVAL,
    DOMAIN,
    signal_updated,
)
from .immich import Immich, ImmichError
from .kidsnote import Kidsnote, KidsnoteAuthError, KidsnoteError, Post
from .sync import Syncer

LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type KidsnoteConfigEntry = ConfigEntry[KidsnoteRunner]


class KidsnoteRunner:
    """Runs one sync at a time in the background and remembers how it went."""

    def __init__(self, hass: HomeAssistant, entry: KidsnoteConfigEntry, syncer: Syncer, store: Store) -> None:
        self.hass = hass
        self.entry = entry
        self.syncer = syncer
        self.store = store
        self.syncing = False
        self.needs_reauth = False
        self.task: asyncio.Task | None = None
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.last_delivered = 0
        self._saved_at = 0.0

    @callback
    def start(self, _now: datetime | None = None) -> None:
        # Retrying a refused password can get the Kidsnote account locked, so
        # wait for the reauth flow (which reloads the entry) instead.
        if not self.syncing and not self.needs_reauth:
            self.syncing = True
            self.task = self.entry.async_create_background_task(self.hass, self._run(), f"{DOMAIN} sync")

    @callback
    def progress(self) -> None:
        """Count a delivered post; at most every 30 s, save state and refresh the sensor."""
        self.last_delivered += 1
        # Throttle rather than debounce: async_delay_save keeps postponing its write
        # while posts stream in, so a crash mid-backfill would lose all progress.
        if (now := time.monotonic()) - self._saved_at >= 30:
            self._saved_at = now
            self.store.async_delay_save(self.syncer.data, 0)
            async_dispatcher_send(self.hass, signal_updated(self.entry.entry_id))

    async def _run(self) -> None:
        self.last_delivered = 0
        async_dispatcher_send(self.hass, signal_updated(self.entry.entry_id))
        try:
            await self.syncer.run()
        except KidsnoteAuthError as err:
            self.last_error = str(err)
            self.needs_reauth = True
            self.entry.async_start_reauth(self.hass)
        except (KidsnoteError, ImmichError, HomeAssistantError, ClientError, OSError) as err:
            LOGGER.warning("Kidsnote sync stopped, retrying next interval: %s", err)
            self.last_error = str(err) or type(err).__name__
        except Exception as err:
            LOGGER.exception("Unexpected error during Kidsnote sync")
            self.last_error = repr(err)
        else:
            self.last_success = dt_util.utcnow()
            self.last_error = None
        finally:
            self.syncing = False
            self.store.async_delay_save(self.syncer.data, 1)
            async_dispatcher_send(self.hass, signal_updated(self.entry.entry_id))


def _media_source_id(hass: HomeAssistant, path: Path) -> str:
    for source, base in hass.config.media_dirs.items():
        if path.is_relative_to(base):
            return f"media-source://media_source/{source}/{path.relative_to(base).as_posix()}"
    return str(path)


def _script_deliverer(hass: HomeAssistant, script_entity_id: str):
    service = script_entity_id.removeprefix("script.")

    async def deliver(post: Post, paths: list[Path], backfill: bool, new: bool) -> None:
        response = await hass.services.async_call(
            "script",
            service,
            {
                "child": post.child,
                "kind": post.kind,
                "id": post.id,
                "date": post.when.isoformat(),
                "title": post.title,
                "text": post.text,
                "author": post.author,
                "files": [_media_source_id(hass, path) for path in paths],
                "paths": [str(path) for path in paths],
                "backfill": backfill,
                "new": new,
            },
            blocking=True,
            return_response=True,
        )
        # A script that stops early (stop/condition) still "succeeds" for the caller,
        # so only an explicit {ok: true} marks the post delivered.
        if not (response or {}).get("ok"):
            raise HomeAssistantError(f"{script_entity_id} did not return {{ok: true}}")

    return deliver


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def sync_now(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            entry.runtime_data.start()

    hass.services.async_register(DOMAIN, "sync", sync_now)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: KidsnoteConfigEntry) -> bool:
    options = entry.options
    session = async_create_clientsession(hass, cookie_jar=DummyCookieJar())
    immich = (
        Immich(session, options[CONF_IMMICH_URL], options[CONF_IMMICH_API_KEY])
        if options.get(CONF_IMMICH_URL)
        else None
    )
    store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
    media_base = Path(next(iter(hass.config.media_dirs.values()), hass.config.path("media")))
    syncer = Syncer(
        Kidsnote(session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]),
        media_base / DOMAIN,
        await store.async_load() or {},
        immich=immich,
        album=options.get(CONF_ALBUM, DEFAULT_ALBUM),
        deliver=_script_deliverer(hass, options[CONF_SCRIPT]) if options.get(CONF_SCRIPT) else None,
        run_io=hass.async_add_executor_job,
    )
    runner = KidsnoteRunner(hass, entry, syncer, store)
    syncer.on_progress = runner.progress
    entry.runtime_data = runner

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    interval = timedelta(minutes=options.get(CONF_INTERVAL, DEFAULT_INTERVAL))
    entry.async_on_unload(async_track_time_interval(hass, runner.start, interval))
    runner.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KidsnoteConfigEntry) -> bool:
    runner = entry.runtime_data
    # Stop a running sync and write its progress now: a reload (reauth, options)
    # reads the state back right away, before a delayed save would land.
    if runner.task and not runner.task.done():
        runner.task.cancel()
        await asyncio.wait([runner.task])
    await runner.store.async_save(runner.syncer.data())
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

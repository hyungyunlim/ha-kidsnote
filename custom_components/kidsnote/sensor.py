"""Last-sync sensor: when the last pass succeeded, and why the latest one failed."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KidsnoteConfigEntry
from .const import DOMAIN, signal_updated


async def async_setup_entry(
    hass: HomeAssistant, entry: KidsnoteConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([KidsnoteLastSyncSensor(entry)])


class KidsnoteLastSyncSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "last_sync"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = False

    def __init__(self, entry: KidsnoteConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_sync"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Kidsnote {entry.title}",
            manufacturer="Kidsnote",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_updated(self._entry.entry_id), self.async_write_ha_state)
        )

    @property
    def native_value(self) -> datetime | None:
        return self._entry.runtime_data.last_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        runner = self._entry.runtime_data
        return {
            "syncing": runner.syncing,
            "last_error": runner.last_error,
            "last_delivered": runner.last_delivered,
            "backfill_done": sorted(runner.syncer.complete),
        }

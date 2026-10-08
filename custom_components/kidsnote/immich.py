"""Just enough of the Immich API to upload, describe and file assets into albums."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

import aiohttp

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


class ImmichError(Exception):
    """Immich refused a request. ``status`` is the HTTP status, 0 if unreachable."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class Immich:
    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str) -> None:
        self._session = session
        self._base = url.rstrip("/") + "/api"
        self._headers = {"x-api-key": api_key, "Accept": "application/json"}
        self._albums: dict[str, str] = {}

    async def _call(self, method: str, path: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
        try:
            async with self._session.request(
                method, self._base + path, headers={**self._headers, **(headers or {})}, **kwargs
            ) as resp:
                if resp.status >= 400:
                    raise ImmichError(f"{method} {path}: HTTP {resp.status} {(await resp.text())[:200]}", resp.status)
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ImmichError(f"{method} {path}: {err}") from err

    async def albums(self) -> list[dict[str, Any]]:
        return await self._call("GET", "/albums")

    async def album(self, target: str, child: str) -> str:
        """Album id for a child's assets.

        ``target`` is an album id picked in the options, used as is so renaming
        the album in Immich changes nothing, or a name template whose "{child}"
        becomes the child's name, found by exact name or created.
        """
        if _UUID.fullmatch(target):
            return target
        name = target.replace("{child}", child)
        if name not in self._albums:
            found = next((a["id"] for a in await self.albums() if a.get("albumName") == name), None)
            self._albums[name] = found or (await self._call("POST", "/albums", json={"albumName": name}))["id"]
        return self._albums[name]

    async def upload(self, data: bytes, filename: str, when: datetime, sha1: str) -> str:
        """Asset id. A file Immich already has (same checksum) returns the existing asset."""
        form = aiohttp.FormData()
        form.add_field("fileCreatedAt", when.isoformat())
        form.add_field("fileModifiedAt", when.isoformat())
        form.add_field("filename", filename)
        # deviceAssetId/deviceId: required by Immich v2, ignored by v3.
        form.add_field("deviceAssetId", f"kidsnote-{sha1}")
        form.add_field("deviceId", "home-assistant-kidsnote")
        form.add_field("assetData", data, filename=filename, content_type="application/octet-stream")
        result = await self._call("POST", "/assets", data=form, headers={"x-immich-checksum": sha1})
        return result["id"]

    async def describe(self, asset_id: str, text: str) -> None:
        await self._call("PUT", f"/assets/{asset_id}", json={"description": text})

    async def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        # Assets already in the album come back as per-id "duplicate" errors; that's fine.
        await self._call("PUT", f"/albums/{album_id}/assets", json={"ids": asset_ids})

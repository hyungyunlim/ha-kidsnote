"""One sync pass: Kidsnote posts → local folder → Immich → delivery script.

Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .immich import Immich
    from .kidsnote import Kidsnote, Post

LOGGER = logging.getLogger(__name__)

KINDS = ("report", "album")
Deliver = Callable[["Post", list[Path], bool, bool], Awaitable[None]]


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _write(path: Path, data: bytes, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class Syncer:
    """Walks each child's reports and albums newest first.

    ``state["done"]`` holds post and media keys already delivered.
    ``state["complete"]`` holds "<child id>:<kind>" once a pass reached the
    oldest page. Until then every page is walked, so an interrupted first run
    (the backfill) resumes; afterwards a pass stops at the first page with
    nothing new.
    """

    def __init__(
        self,
        kidsnote: Kidsnote,
        root: Path,
        state: dict[str, Any],
        *,
        immich: Immich | None = None,
        album_name: str = "Kidsnote - {child}",
        deliver: Deliver | None = None,
        run_io: Callable[..., Awaitable[Any]] | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> None:
        self.kidsnote = kidsnote
        self.root = root
        self.immich = immich
        self.album_name = album_name
        self.deliver = deliver
        self.run_io = run_io or asyncio.to_thread
        self.on_progress = on_progress
        self.done: set[str] = set(state.get("done", []))
        self.complete: set[str] = set(state.get("complete", []))

    def data(self) -> dict[str, list[str]]:
        return {"done": sorted(self.done), "complete": sorted(self.complete)}

    async def run(self) -> int:
        """Sync every child; returns how many posts were delivered."""
        delivered = 0
        for child in await self.kidsnote.children():
            for kind in KINDS:
                delivered += await self._sync(child, kind)
        return delivered

    async def _sync(self, child: dict[str, str], kind: str) -> int:
        tag = f"{child['id']}:{kind}"
        backfill = tag not in self.complete
        delivered = 0
        async with aclosing(self.kidsnote.pages(child, kind)) as pages:
            async for page in pages:
                fresh = 0
                for post in page:
                    todo = [m for m in post.media if m.key not in self.done]
                    if post.key in self.done and not todo:
                        continue
                    await self._deliver(post, todo, backfill)
                    fresh += 1
                delivered += fresh
                if not fresh and not backfill:
                    return delivered
        self.complete.add(tag)
        return delivered

    async def _deliver(self, post: Post, todo: list, backfill: bool) -> None:
        child_dir = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", post.child)
        folder = self.root / child_dir / f"{post.when:%Y-%m}" / f"{post.when:%Y-%m-%d}_{post.kind}_{post.id}"
        text = "\n\n".join(part for part in (post.title.strip(), post.text.strip()) if part)
        is_new = post.key not in self.done
        paths: list[Path] = []
        asset_ids: list[str] = []

        # ponytail: one file in memory at a time; Kidsnote videos are tens of MB at most.
        for media in todo:
            path = folder / media.filename
            data = await self.run_io(_read, path)
            if data is None:
                data = await self.kidsnote.download(media.url)
                if data is None:
                    LOGGER.warning("Kidsnote no longer serves %s; skipping it", media.key)
                    self.done.add(media.key)
                    continue
                await self.run_io(_write, path, data, post.when.timestamp())
            paths.append(path)
            if self.immich:
                sha1 = await self.run_io(_sha1, data)
                asset_ids.append(await self.immich.upload(data, path.name, post.when, sha1))

        record = {
            "kind": post.kind,
            "id": post.id,
            "child": post.child,
            "date": post.when.isoformat(),
            "title": post.title,
            "text": post.text,
            "author": post.author,
            "files": [m.filename for m in post.media],
        }
        await self.run_io(_write, folder / "post.json", json.dumps(record, ensure_ascii=False, indent=2).encode())

        if asset_ids:
            if text:
                for asset_id in asset_ids:
                    await self.immich.describe(asset_id, text)
            album_id = await self.immich.album(self.album_name.format(child=post.child))
            await self.immich.add_to_album(album_id, asset_ids)

        if self.deliver:
            await self.deliver(post, paths, backfill, is_new)

        self.done.add(post.key)
        self.done.update(m.key for m in todo)
        if self.on_progress:
            self.on_progress()

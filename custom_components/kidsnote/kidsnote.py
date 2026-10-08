"""Kidsnote client for the unofficial JSON API that kidsnote.com's web app uses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse

import aiohttp

BASE = "https://www.kidsnote.com"
KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko",
}
PAGE_SIZE = 30
LOGIN_BUSY_DELAYS = (5, 10, 20)
# kidsnote.com's login form lower-cases/trims the id and strips whitespace and
# Korean letters from the password before sending; do the same.
_PASSWORD_DROP = re.compile(r"[\s﻿ㄱ-ㅎㅏ-ㅣ가-힣]")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class KidsnoteAuthError(Exception):
    """Login refused. ``reason`` is "invalid_auth", "blocked" or "two_factor"."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Kidsnote login failed: {reason}")
        self.reason = reason


class KidsnoteError(Exception):
    """Kidsnote answered something unexpected. Worth retrying later."""


@dataclass
class Media:
    key: str
    url: str
    filename: str


@dataclass
class Post:
    key: str
    kind: str  # "report" (알림장) or "album" (앨범)
    id: str
    child: str
    when: datetime
    title: str
    text: str
    author: str
    media: list[Media] = field(default_factory=list)


async def _json(resp: aiohttp.ClientResponse) -> Any:
    try:
        return await resp.json(content_type=None)
    except ValueError:
        return None


def _when(raw: dict[str, Any]) -> datetime:
    for key in ("created", "date_written"):
        try:
            value = datetime.fromisoformat(str(raw[key]).replace(" ", "T"))
        except (KeyError, ValueError):
            continue
        return value if value.tzinfo else value.replace(tzinfo=KST)
    return datetime.now(KST)


def _media(post_key: str, raw: dict[str, Any]) -> list[Media]:
    items = [("img", item) for item in raw.get("attached_images") or [] if isinstance(item, dict)]
    if isinstance(video := raw.get("attached_video"), dict):
        items.append(("vid", video))

    media = []
    for n, (tag, item) in enumerate(items, 1):
        url = next((item[k] for k in ("original", "high", "large", "low") if item.get(k)), None)
        if not url:
            continue
        name = _UNSAFE_NAME.sub("_", str(item.get("original_file_name") or "").strip()) or tag
        if not PurePosixPath(name).suffix:
            name += PurePosixPath(urlparse(url).path).suffix or (".mp4" if tag == "vid" else ".jpg")
        media.append(Media(f"{post_key}:{tag}:{item.get('id') or n}", url, f"{n:02d}-{name}"))
    return media


def _post(kind: str, child: str, raw: dict[str, Any]) -> Post:
    key = f"{kind}:{raw['id']}"
    author = raw.get("author")
    return Post(
        key=key,
        kind=kind,
        id=str(raw["id"]),
        child=child,
        when=_when(raw),
        title=str(raw.get("title") or ""),
        text=str(raw.get("content") or ""),
        author=str(raw.get("author_name") or (author.get("name") if isinstance(author, dict) else "") or ""),
        media=_media(key, raw),
    )


class Kidsnote:
    """Logs in with id/password and logs in again whenever the session dies.

    The ``sessionid`` cookie lasts 14 days, so storing cookies alone always
    breaks; a fresh login does not sign out the phone app or browser.
    """

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        # The sessionid travels in an explicit header, so give this a session
        # with aiohttp.DummyCookieJar() to keep it out of any shared jar.
        self._session = session
        self._username = username.strip().lower()[:32]
        self._password = _PASSWORD_DROP.sub("", password)
        self._sessionid: str | None = None

    async def login(self) -> None:
        self._sessionid = None
        async with self._session.get(
            f"{BASE}/api/v1/second-factors/{quote(self._username, safe='')}/", headers=HEADERS
        ) as resp:
            info = await _json(resp) if resp.status == 200 else None
        if isinstance(info, dict) and info.get("is_enabled") and info.get("second_factors"):
            raise KidsnoteAuthError("two_factor")

        login_headers = {**HEADERS, "Origin": BASE, "Referer": f"{BASE}/kr/login"}
        body = {"username": self._username, "password": self._password, "remember_me": True}
        for delay in (*LOGIN_BUSY_DELAYS, None):
            async with self._session.post(f"{BASE}/api/web/login/", json=body, headers=login_headers) as resp:
                status, answer, cookie = resp.status, await _json(resp), resp.cookies.get("sessionid")
            # 409 = another login for this account is in flight; it clears in seconds.
            if status != 409 or delay is None:
                break
            await asyncio.sleep(delay)

        if status in (400, 401, 403, 404):
            blocked = isinstance(answer, dict) and answer.get("err_code") == "blocked"
            raise KidsnoteAuthError("blocked" if blocked else "invalid_auth")
        if status != 200:
            raise KidsnoteError(f"login: HTTP {status}")
        if cookie is None or not cookie.value:
            raise KidsnoteError("login: no sessionid cookie")
        self._sessionid = cookie.value

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for _ in range(2):
            if not self._sessionid:
                await self.login()
            headers = {**HEADERS, "Cookie": f"sessionid={self._sessionid}"}
            async with self._session.get(BASE + path, params=params, headers=headers) as resp:
                if resp.status in (401, 403) or "json" not in resp.content_type:
                    self._sessionid = None  # session expired; log in again
                    continue
                if resp.status != 200:
                    raise KidsnoteError(f"{path}: HTTP {resp.status}")
                return await resp.json()
        raise KidsnoteError(f"{path}: rejected right after logging in")

    async def children(self) -> list[dict[str, str]]:
        body = await self._get("/api/v1/me/children/")
        rows = body if isinstance(body, list) else body.get("results") or body.get("children") or []
        return [
            {"id": str(row["id"]), "name": str(row["name"])}
            for row in rows
            if isinstance(row, dict) and row.get("id") and row.get("name")
        ]

    async def pages(self, child: dict[str, str], kind: str) -> AsyncIterator[list[Post]]:
        """Yield a child's reports or albums one page at a time, newest first."""
        path = f"/api/v1_2/children/{child['id']}/{kind}s/"
        params: dict[str, Any] = {"page_size": PAGE_SIZE, "tz": "Asia/Seoul"}
        if kind == "report":
            params["child"] = child["id"]
        seen: set[str] = set()
        while True:
            body = await self._get(path, params)
            rows = [row for row in body.get("results") or [] if isinstance(row, dict) and row.get("id")]
            # Reports a parent sends to the daycare hold the parent's own photos.
            rows = [row for row in rows if row.get("is_sent_from_center", True)]
            yield [_post(kind, child["name"], row) for row in rows]

            nxt = body.get("next")
            if not nxt or str(nxt) in seen:
                return
            seen.add(str(nxt))
            if str(nxt).startswith("http"):
                url = urlparse(str(nxt))
                path, params = url.path, dict(parse_qsl(url.query))
            else:
                params = {**params, "page": nxt}

    async def download(self, url: str) -> bytes | None:
        """File bytes, or None when the CDN no longer has the file."""
        async with self._session.get(url, headers={"User-Agent": HEADERS["User-Agent"]}) as resp:
            if resp.status in (403, 404, 410):
                return None
            resp.raise_for_status()
            return await resp.read()

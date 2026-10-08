"""Sync pass checks with fake Kidsnote/Immich. Run: python tests/test_sync.py (or pytest)."""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS

_spec = importlib.util.spec_from_file_location(
    "kidsnote_sync", Path(__file__).parents[1] / "custom_components/kidsnote/sync.py"
)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

KST = timezone(timedelta(hours=9))
CHILD = {"id": "7", "name": "하늘"}


def post(kind, pid, day, n_media=1, text="오늘도 즐거웠어요"):
    key = f"{kind}:{pid}"
    media = [
        NS(key=f"{key}:img:{pid}{i}", url=f"https://cdn/{pid}/{i}", filename=f"{i:02d}-p.jpg")
        for i in range(1, n_media + 1)
    ]
    return NS(key=key, kind=kind, id=str(pid), child="하늘", when=datetime(2026, 9, day, 15, tzinfo=KST),
              title="", text=text, author="선생님", media=media)


class FakeKidsnote:
    def __init__(self, pages):
        self.pages_by_kind = pages  # kind -> list of pages, newest first
        self.fetched = []
        self.gone = set()
        self.fail_on_page = None

    async def children(self):
        return [CHILD]

    async def pages(self, child, kind):
        for i, page in enumerate(self.pages_by_kind.get(kind, [])):
            if self.fail_on_page == (kind, i):
                raise OSError("network down")
            self.fetched.append((kind, i))
            yield page

    async def download(self, url):
        return None if url in self.gone else f"bytes of {url}".encode()


class FakeImmich:
    def __init__(self, existing=()):
        self.assets = {sync._sha1(b): f"old-{i}" for i, b in enumerate(existing)}
        self.uploads, self.descriptions, self.album_adds = [], {}, []

    async def upload(self, data, filename, when, sha1):
        self.uploads.append(filename)
        return self.assets.setdefault(sha1, f"asset-{len(self.assets)}")

    async def describe(self, asset_id, text):
        self.descriptions[asset_id] = text

    async def album(self, target, child):
        assert (target, child) == ("Kidsnote - {child}", "하늘")
        return "album-1"

    async def add_to_album(self, album_id, ids):
        self.album_adds.append(list(ids))


def make(tmp, kn, immich, state=None, deliver=None):
    return sync.Syncer(kn, Path(tmp), state or {}, immich=immich, deliver=deliver)


def run(coro):
    return asyncio.run(coro)


def test_backfill_then_incremental():
    with tempfile.TemporaryDirectory() as tmp:
        reports = [[post("report", 3, 28, 2), post("report", 2, 27)], [post("report", 1, 26, text="")]]
        albums = [[post("album", 9, 25, 3)]]
        kn = FakeKidsnote({"report": reports, "album": albums})
        # Social Archiver already uploaded report 2's photo: Immich must answer with that asset.
        immich = FakeImmich(existing=[b"bytes of https://cdn/2/1"])
        calls = []

        async def deliver(p, paths, backfill, new):
            calls.append((p.key, len(paths), backfill, new))

        s = make(tmp, kn, immich, deliver=deliver)
        assert run(s.run()) == 4
        assert len(immich.uploads) == 7
        assert immich.descriptions["old-0"] == "오늘도 즐거웠어요"  # existing asset got the report text
        assert len(immich.descriptions) == 6  # report 1 has no text, so its photo keeps none
        assert s.complete == {"7:report", "7:album"}
        assert all(c[2] and c[3] for c in calls)

        folder = Path(tmp) / "하늘" / "2026-09" / "2026-09-28_report_3"
        assert json.loads((folder / "post.json").read_text())["text"] == "오늘도 즐거웠어요"
        photo = folder / "01-p.jpg"
        assert photo.stat().st_mtime == datetime(2026, 9, 28, 15, tzinfo=KST).timestamp()

        # Nothing new: one page per kind, no uploads.
        kn.fetched.clear()
        assert run(s.run()) == 0
        assert kn.fetched == [("report", 0), ("album", 0)]
        assert len(immich.uploads) == 7

        # A new report on top and a teacher adding a photo to report 3.
        edited = post("report", 3, 28, 3)
        reports[0] = [post("report", 4, 29), edited, reports[0][1]]
        calls.clear()
        assert run(s.run()) == 2
        assert immich.uploads[7:] == ["01-p.jpg", "03-p.jpg"]
        assert calls == [("report:4", 1, False, True), ("report:3", 1, False, False)]


def test_interrupted_backfill_resumes():
    with tempfile.TemporaryDirectory() as tmp:
        kn = FakeKidsnote({"report": [[post("report", 2, 27)], [post("report", 1, 26)]], "album": []})
        immich = FakeImmich()
        kn.fail_on_page = ("report", 1)
        s = make(tmp, kn, immich)
        with suppress(OSError):
            run(s.run())
        assert s.complete == set() and "report:2" in s.done

        # Restart from saved state: page 0 is already done but the walk continues to page 1.
        kn.fail_on_page = None
        s = make(tmp, kn, immich, state=s.data())
        assert run(s.run()) == 1
        assert immich.uploads == ["01-p.jpg", "01-p.jpg"]
        assert "7:report" in s.complete


def test_gone_media_is_skipped_and_failed_delivery_retried():
    with tempfile.TemporaryDirectory() as tmp:
        p = post("report", 5, 20, 2)
        kn = FakeKidsnote({"report": [[p]]})
        kn.gone.add("https://cdn/5/2")
        immich = FakeImmich()
        attempts = []

        async def deliver(p, paths, backfill, new):
            attempts.append(len(paths))
            if len(attempts) == 1:
                raise RuntimeError("script did not return ok")

        s = make(tmp, kn, immich, deliver=deliver)
        with suppress(RuntimeError):
            run(s.run())
        assert "report:5" not in s.done and "report:5:img:52" in s.done

        assert run(s.run()) == 1  # retried; the kept file is reused, not downloaded again
        assert attempts == [1, 1] and "report:5" in s.done


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    sys.exit(0)

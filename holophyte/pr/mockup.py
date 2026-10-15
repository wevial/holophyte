"""The approved mock-up's snapshots and bundles, fetched into the task worktree."""
import http.client
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import store
import ticket_template
from holophyte.config.config_tables import merge_config

DIRECTORY = ".holophyte/mockup"
MOCKUP_IMAGE_MAX_MB = 10
MOCKUP_BUNDLE_MAX_MB = 25
BUNDLE_MAX_MEMBERS = 200
BUNDLE_MAX_UNPACKED_MB = 50
TIMEOUT_S = 30
MB = 1024 * 1024
CHUNK = 64 * 1024
MEMBER_SUFFIXES = (".png", ".html", ".json")
KINDS = {"snapshot": ("image/png", b"\x89PNG\r\n\x1a\n", MOCKUP_IMAGE_MAX_MB),
         "bundle": ("application/zip", b"PK\x03\x04", MOCKUP_BUNDLE_MAX_MB)}
NO_MEDIA = ("mock-up: no snapshot or bundle; the implementer has only the "
            "design link, which it cannot open")
BRIEF_HEAD = (
    "\n\nThe approved design for this ticket, downloaded by the factory into "
    f"`{DIRECTORY}/` (ignored; never commit, move or edit these files). Match "
    "it: layout, copy, states. Open every image before writing UI code.\n")
BRIEF_TAIL = (
    "\nRead each board's .dc.html for its exact copy, structure and styles. "
    "It is design source, not code to paste: build it from this repository's "
    "own components and styles. Where the ticket text and the design "
    "disagree, the ticket text wins; name each such difference in your "
    "final reply.")


class Link(NamedTuple):
    kind: str
    url: str
    caption: str


class Refused(Exception):
    pass


def media_links(source):
    links = []
    for line in source.splitlines():
        for url in ticket_template._mockup_urls(line):
            kind = ticket_template.mockup_media_kind(url)
            if kind:
                links.append(Link(kind, url, _caption(line, url)))
    return links


def _caption(line, url):
    end = line.find(f"]({url})")
    start = line.rfind("[", 0, max(end, 0))
    caption = line[start + 1:end].strip() if end >= 0 and start >= 0 else ""
    return caption or unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def _public_base(cfg):
    return cfg.media_bucket["public_base"].rstrip("/") if cfg.media_bucket else None


def _under(url, base):
    if not url.startswith(base + "/"):
        return False
    rest = urlsplit(url[len(base):])
    return not rest.query and not rest.fragment and not {".", ".."} & {
        unquote(part) for part in rest.path.split("/")}


def filing_problems(project, ticket):
    if "Mock-up" not in ticket.order:
        return []
    try:
        cfg = merge_config(project)
    except SystemExit:
        return []
    base, links = _public_base(cfg), media_links(ticket.mockup_source)
    if base is None:
        return [f"'## Mock-up' media links are read only from "
                f"[merge.media_bucket], which this project does not set: "
                f"{link.url}" for link in links]
    problems = [f"'## Mock-up' media link is not under the project's media "
                f"bucket (public_base {base}): {link.url}"
                for link in links if not _under(link.url, base)]
    if (not links and cfg.ui_paths
            and (ticket.ui_change or "").lower() == "major"):
        problems.append(
            "'**UI change:** major' needs at least one snapshot or mock-up "
            "bundle under '## Mock-up': export the approved boards as PNGs, "
            "or as a .zip of their .dc.html sources, canvas.json and PNGs, "
            f"upload them under {base}, and add each below the Approved line "
            "as ![caption](URL)")
    return problems


def _record(conn, run_id, summary):
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "mockup", summary)


def fetch(project, conn, run_id, wt, body):
    ticket = ticket_template.parse(body)
    if "Mock-up" not in ticket.order:
        return
    links = media_links(ticket.mockup_source)
    if not links:
        if (ticket.ui_change or "").lower() == "major":
            _record(conn, run_id, NO_MEDIA)
        return
    try:
        base = _public_base(merge_config(project))
        _refuse_unsafe(Path(wt))
        items = _rebuild(Path(wt), links, base,
                         lambda summary: _record(conn, run_id, summary))
    except (Refused, SystemExit, OSError, subprocess.CalledProcessError) as error:
        _record(conn, run_id, f"mock-up: nothing fetched: {error}")
        return
    if not items:
        return
    snapshots = [item for item in items if item["kind"] == "snapshot"]
    bundles = [item for item in items if item["kind"] == "bundle"]
    boards = sum(len(item["boards"]) for item in bundles)
    _record(conn, run_id, f"mock-up: {len(snapshots)} snapshots and "
            f"{len(bundles)} bundles ({boards} boards) in {DIRECTORY}")


def _refuse_unsafe(root):
    for path in (root / DIRECTORY).parent, root / DIRECTORY:
        if path.is_symlink():
            raise Refused(f"{path.relative_to(root)} is a symlink")
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", str(PurePosixPath(DIRECTORY).parent)],
        cwd=root, capture_output=True, text=True, check=True).stdout
    for name in filter(None, tracked.split("\0")):
        if name in (".holophyte", DIRECTORY) or name.startswith(DIRECTORY + "/"):
            raise Refused(f"{name} is tracked by git")


def _rebuild(root, links, base, warn):
    target = root / DIRECTORY
    target.parent.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".mockup-", dir=target.parent))
    try:
        staging.chmod(0o755)
        (staging / ".gitignore").write_text("*\n")
        items = []
        for number, link in enumerate(links, 1):
            try:
                items.append(_fetch_one(staging, number, link, base))
            except Refused as why:
                warn(f"mock-up: {link.url} not fetched: {why}")
        (staging / "manifest.json").write_text(
            json.dumps({"items": items}, indent=2) + "\n")
        _swap(staging, target)
        return items
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _swap(staging, target):
    old = None
    if target.exists():
        old = Path(tempfile.mkdtemp(prefix=".mockup-old-", dir=target.parent))
        os.rename(target, old / "mockup")
    os.rename(staging, target)
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def _fetch_one(staging, number, link, base):
    if base is None:
        raise Refused("this project sets no [merge.media_bucket]")
    if not _under(link.url, base):
        raise Refused(f"not under the media bucket's public_base {base}")
    data = _download(link, base)
    item = {"kind": link.kind, "caption": link.caption, "url": link.url}
    if link.kind == "snapshot":
        name = f"{number:02d}.png"
        (staging / name).write_bytes(data)
        return {**item, "path": f"{DIRECTORY}/{name}"}
    name = f"bundle-{number:02d}"
    try:
        members = _unpack(data, staging / name)
    except Refused:
        shutil.rmtree(staging / name, ignore_errors=True)
        raise
    paths = [f"{DIRECTORY}/{name}/{member}" for member in members]
    return {**item, "path": f"{DIRECTORY}/{name}",
            "boards": [path for path in paths if path.endswith(".dc.html")],
            "images": [path for path in paths if path.endswith(".png")]}


class _StayUnder(HTTPRedirectHandler):
    def __init__(self, base):
        self.base = base

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _under(newurl, self.base):
            raise Refused(f"redirected off the media bucket to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(link, base):
    content_type, signature, cap_mb = KINDS[link.kind]
    cap, deadline = cap_mb * MB, time.monotonic() + TIMEOUT_S
    expired = threading.Event()
    try:
        with build_opener(_StayUnder(base)).open(
                Request(link.url), timeout=TIMEOUT_S) as response:
            timer = threading.Timer(max(deadline - time.monotonic(), 0),
                                    _expire, (response, expired))
            timer.start()
            try:
                data, length = _receive(response, link, cap, expired)
            finally:
                timer.cancel()
    except HTTPError as error:
        raise Refused(f"HTTP {error.code}") from None
    except (URLError, OSError, ValueError, http.client.HTTPException) as error:
        if expired.is_set():
            raise Refused(f"took longer than {TIMEOUT_S} s") from None
        if isinstance(error, (URLError, OSError)):
            raise Refused(str(getattr(error, "reason", error))) from None
        raise Refused(f"cannot be requested ({type(error).__name__})") from None
    if expired.is_set():
        raise Refused(f"took longer than {TIMEOUT_S} s")
    if length.isdigit() and len(data) != int(length):
        raise Refused(f"ended after {len(data)} of {length} bytes")
    if not data.startswith(signature):
        raise Refused(f"does not start with the {content_type} signature")
    return bytes(data)


def _expire(response, expired):
    expired.set()
    sock = getattr(getattr(response.fp, "raw", None), "_sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _receive(response, link, cap, expired):
    content_type, _, cap_mb = KINDS[link.kind]
    served = response.headers.get_content_type()
    if served != content_type:
        raise Refused(f"served as {served}, not {content_type}")
    length = response.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > cap:
        raise Refused(f"larger than {cap_mb} MB")
    data = bytearray()
    while not expired.is_set() and (chunk := response.read1(CHUNK)):
        data += chunk
        if len(data) > cap:
            raise Refused(f"larger than {cap_mb} MB")
    return data, length


def _unpack(data, directory):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            infos = bundle.infolist()
            if len(infos) > BUNDLE_MAX_MEMBERS:
                raise Refused(f"holds more than {BUNDLE_MAX_MEMBERS} members")
            if sum(info.file_size for info in infos) > BUNDLE_MAX_UNPACKED_MB * MB:
                raise Refused(f"unpacks to more than {BUNDLE_MAX_UNPACKED_MB} MB")
            files = [info for info in infos if not _checked_dir(info)]
            directory.mkdir()
            budget = BUNDLE_MAX_UNPACKED_MB * MB
            for info in files:
                budget = _extract(bundle, info, directory, budget)
            return [info.filename for info in files]
    except (zipfile.BadZipFile, RuntimeError, ValueError, NotImplementedError,
            EOFError, OSError, zlib.error) as error:
        raise Refused(f"is not a readable zip: {error}") from None


def _checked_dir(info):
    name, kind = info.filename, stat.S_IFMT(info.external_attr >> 16)
    path = PurePosixPath(name)
    if name.startswith("/") or "\\" in name or ".." in path.parts or not path.parts:
        raise Refused(f"member {name!r} is not a plain relative name")
    if info.is_dir():
        if kind not in (0, stat.S_IFDIR):
            raise Refused(f"member {name!r} is not a directory")
        return True
    if kind not in (0, stat.S_IFREG):
        raise Refused(f"member {name!r} is not a regular file")
    if not name.endswith(MEMBER_SUFFIXES):
        raise Refused(f"member {name!r} is not a .png, .html or .json file")
    return False


def _extract(bundle, info, directory, budget):
    target = directory.joinpath(*PurePosixPath(info.filename).parts)
    if not target.resolve().is_relative_to(directory.resolve()):
        raise Refused(f"member {info.filename!r} resolves outside the bundle")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as out, bundle.open(info) as source:
        while chunk := source.read(CHUNK):
            budget -= len(chunk)
            if budget < 0:
                raise Refused(f"unpacks to more than {BUNDLE_MAX_UNPACKED_MB} MB")
            out.write(chunk)
    return budget


def implementer_brief(wt):
    directory = Path(wt) / DIRECTORY
    try:
        _refuse_unsafe(Path(wt))
        items = json.loads((directory / "manifest.json").read_text())["items"]
        images = [f"- `{item['path']}`: {item['caption']}" for item in items
                  if item["kind"] == "snapshot"]
        bundles = [item for item in items if item["kind"] == "bundle"]
        images += [f"- `{path}`: {item['caption']}" for item in bundles
                   for path in item["images"]]
        boards = [f"- `{path}`: {item['caption']}" for item in bundles
                  for path in item["boards"]]
    except (Refused, OSError, subprocess.CalledProcessError, ValueError,
            KeyError, TypeError):
        return ""
    if not images and not boards:
        return ""
    return (BRIEF_HEAD + "\n".join(images + boards) + "\n" + BRIEF_TAIL)

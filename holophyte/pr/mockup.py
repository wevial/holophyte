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
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

import store
import ticket_template
from holophyte.config.config_tables import merge_config

DIRECTORY = ".holophyte/mockup"
CAPTURED = ".holophyte/captured"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MOCKUP_IMAGE_MAX_MB = 10
MOCKUP_BUNDLE_MAX_MB = 25
BUNDLE_MAX_MEMBERS = 200
BUNDLE_MAX_UNPACKED_MB = 50
TIMEOUT_S = 30
USER_AGENT = "holophyte"
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

DESIGN_CHECK = (
    "Design-match check: open every approved image and every captured "
    "screenshot. A visible difference between the candidate's UI and the "
    "approved design (layout, copy, a missing or extra state, a control's "
    "place or style) is a finding unless the ticket text asks for it; name "
    "the board or snapshot and the screenshot. Where no screenshot shows a "
    "board, compare the diff's markup and copy with the board's .dc.html "
    "source.\n\n")


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
    parts = _split(url)
    return caption or unquote((parts.path if parts else url).rsplit("/", 1)[-1])


def _split(url):
    try:
        return urlsplit(url)
    except ValueError:
        return None


def _public_base(cfg):
    return cfg.media_bucket["public_base"].rstrip("/") if cfg.media_bucket else None


def _under(url, base):
    rest = _split(url)
    if rest is None or not url.startswith(base + "/"):
        return False
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


def _refuse_unsafe(root, directory=DIRECTORY):
    for path in (root / directory).parent, root / directory:
        if path.is_symlink():
            raise Refused(f"{path.relative_to(root)} is a symlink")
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", str(PurePosixPath(directory).parent)],
        cwd=root, capture_output=True, text=True, check=True).stdout
    for name in filter(None, tracked.split("\0")):
        if name in (".holophyte", directory) or name.startswith(directory + "/"):
            raise Refused(f"{name} is tracked by git")


def _ignored_staging(target):
    target.parent.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    staging.chmod(0o755)
    (staging / ".gitignore").write_text("*\n")
    return staging


def _rebuild(root, links, base, warn):
    target = root / DIRECTORY
    staging = _ignored_staging(target)
    try:
        items = []
        for number, link in enumerate(links, 1):
            try:
                items.append(_fetch_one(staging, number, link, base))
            except (Refused, ValueError) as why:
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
        old = Path(tempfile.mkdtemp(prefix=f".{target.name}-old-",
                                    dir=target.parent))
        os.rename(target, old / target.name)
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


def _download(link, base, cap_mb=None):
    content_type, signature, kind_cap_mb = KINDS[link.kind]
    cap_mb = cap_mb or kind_cap_mb
    watch = _Watch()
    timer = threading.Timer(TIMEOUT_S, watch.expire)
    timer.start()
    try:
        with build_opener(_StayUnder(base), _HTTP(watch), _HTTPS(watch)).open(
                Request(link.url, headers={"User-Agent": USER_AGENT}),
                timeout=TIMEOUT_S) as response:
            data, length = _receive(response, link, cap_mb, watch.expired)
    except HTTPError as error:
        raise Refused(f"HTTP {error.code}") from None
    except (URLError, OSError, ValueError, http.client.HTTPException) as error:
        if watch.expired.is_set():
            raise Refused(f"took longer than {TIMEOUT_S} s") from None
        if isinstance(error, (URLError, OSError)):
            raise Refused(str(getattr(error, "reason", error))) from None
        raise Refused(f"cannot be requested ({type(error).__name__})") from None
    finally:
        timer.cancel()
    if watch.expired.is_set():
        raise Refused(f"took longer than {TIMEOUT_S} s")
    if length.isdigit() and len(data) != int(length):
        raise Refused(f"ended after {len(data)} of {length} bytes")
    if not data.startswith(signature):
        raise Refused(f"does not start with the {content_type} signature")
    return bytes(data)


class _Watch:
    def __init__(self):
        self.expired, self.lock, self.sockets = threading.Event(), threading.Lock(), []

    def connection(self, base_class):
        watch = self

        class Connection(base_class):
            def connect(self):
                super().connect()
                watch.add(self.sock)
        return Connection

    def add(self, sock):
        with self.lock:
            self.sockets.append(sock)
            if self.expired.is_set():
                _shutdown(sock)

    def expire(self):
        with self.lock:
            self.expired.set()
            for sock in self.sockets:
                _shutdown(sock)


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _HTTP(HTTPHandler):
    def __init__(self, watch):
        super().__init__()
        self.watch = watch

    def http_open(self, req):
        return self.do_open(self.watch.connection(http.client.HTTPConnection), req)


class _HTTPS(HTTPSHandler):
    def __init__(self, watch):
        super().__init__()
        self.watch = watch

    def https_open(self, req):
        return self.do_open(self.watch.connection(http.client.HTTPSConnection),
                            req, context=self._context)


def _receive(response, link, cap_mb, expired):
    content_type, cap = KINDS[link.kind][0], cap_mb * MB
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


def _design(wt):
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
        return []
    return images + boards


def implementer_brief(wt):
    design = _design(wt)
    return BRIEF_HEAD + "\n".join(design) + "\n" + BRIEF_TAIL if design else ""


def clear_captured(wt):
    target = Path(wt) / CAPTURED
    try:
        _refuse_unsafe(Path(wt), CAPTURED)
        staging = _ignored_staging(target)
        try:
            (staging / ".head").write_text(_head(wt))
            _swap(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    except (Refused, OSError, subprocess.CalledProcessError):
        return False
    return True


def _head(wt):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True,
                          text=True, check=True).stdout.strip()


def keep_captured(wt, output, files):
    try:
        _refuse_unsafe(Path(wt), CAPTURED)
        parent = os.open(Path(wt) / ".holophyte", DIRECTORY_FLAGS)
        try:
            directory = os.open(Path(CAPTURED).name, DIRECTORY_FLAGS, dir_fd=parent)
        finally:
            os.close(parent)
    except (Refused, OSError, subprocess.CalledProcessError):
        return
    try:
        for file in files:
            if file.suffix == ".png":
                _copy_new(file, file.relative_to(output).parts, directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def _copy_new(file, parts, directory):
    opened = []
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=directory)
            except FileExistsError:
                pass
            directory = os.open(part, DIRECTORY_FLAGS, dir_fd=directory)
            opened.append(directory)
        try:
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o644, dir_fd=directory)
        except FileExistsError:
            return
        with os.fdopen(fd, "wb") as out, open(file, "rb") as source:
            shutil.copyfileobj(source, out)
    finally:
        for fd in opened:
            os.close(fd)


def review_carry(wt, configured=()):
    root = Path(wt)
    try:
        _refuse_unsafe(root)
        _refuse_unsafe(root, CAPTURED)
    except (Refused, OSError, subprocess.CalledProcessError):
        return []
    taken = [Path(entry).parts for entry in configured]
    return [entry for entry in (DIRECTORY, CAPTURED) if (root / entry).is_dir()
            and not any(_nested(Path(entry).parts, other) for other in taken)]


def _nested(one, other):
    shorter = min(len(one), len(other))
    return one[:shorter] == other[:shorter]


def release_review_carry(checkout):
    for entry in review_carry(checkout):
        for root, _, _ in os.walk(Path(checkout) / entry):
            os.chmod(root, os.stat(root).st_mode | stat.S_IWUSR)


def _captured(wt):
    directory = Path(wt) / CAPTURED
    try:
        _refuse_unsafe(Path(wt), CAPTURED)
        if (directory / ".head").is_symlink() or (
                (directory / ".head").read_text() != _head(wt)):
            return []
        return sorted((Path(root) / name).relative_to(directory).as_posix()
                      for root, _, names in os.walk(directory) for name in names
                      if name.endswith(".png")
                      and not (Path(root) / name).is_symlink())
    except (Refused, OSError, subprocess.CalledProcessError):
        return []


def reviewer_brief(wt):
    design = _design(wt)
    if not design:
        return ""
    captured = [f"- `{CAPTURED}/{name}`" for name in _captured(wt)]
    shown = ("The candidate's UI as captured at this head:\n"
             + "\n".join(captured) if captured
             else "No UI capture was made at this head.")
    return ("The approved design, downloaded by the factory (read-only):\n"
            + "\n".join(design) + f"\n{shown}\n" + DESIGN_CHECK)

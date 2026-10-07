"""`holo mcp --http`: the tool table over the SDK's Streamable HTTP transport."""
import asyncio
import json
import os
import sys

from holophyte.holo.mcp_server import build, sdk_missing
from holophyte.holo.mcp_tools import READS, TOOLS
from holophyte.holo.transport import LOCAL, TRANSPORT
from holophyte.host.registry import Host, HostError, settings
from holophyte.host.supervisor import factory_revision
from holophyte.serve.serve_watch import (
    CODE_CHECK_SEC,
    DRAIN_SEC,
    CodeWatch,
    Moved,
)
from holophyte.serve.server import (
    ADDRESS_SHAPE,
    MACHINE_TOKEN_KEY,
    authorized,
    load_token,
    parse_address,
)

PATH = "/mcp"
ALLOWED = "POST"


def refusal(status, error=None):
    body = json.dumps({} if error is None else {"error": error}).encode()
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode())]
    if status == 405:
        headers.append((b"allow", ALLOWED.encode()))
    return status, headers, body


def refused(scope, tokens):
    headers = dict(scope["headers"])
    if not authorized(headers.get(b"authorization", b"").decode("latin-1"),
                      tokens):
        return refusal(401)
    if b"origin" in headers:
        return refusal(403, "a request with an Origin header is a browser's;"
                            " MCP clients send none")
    if scope["method"] != ALLOWED:
        return refusal(405, f"{scope['method']} is not served; {PATH} takes"
                            f" {ALLOWED} alone, with no event stream")
    return None


def guarded(app, tokens):
    async def guard(scope, receive, send):
        if scope["type"] not in ("http", "lifespan"):
            return
        answer = refused(scope, tokens) if scope["type"] == "http" else None
        if answer is None:
            return await app(scope, receive, send)
        status, headers, body = answer
        await send({"type": "http.response.start", "status": status,
                    "headers": headers})
        await send({"type": "http.response.body", "body": body})
    return guard


def machine_tokens(host, knobs):
    if knobs.machine_token_file is None:
        raise SystemExit(
            f"[holo2] {host.path}: holo mcp --http needs {MACHINE_TOKEN_KEY}"
            " = \"PATH\" naming a file whose contents every request presents"
            " as `Authorization: Bearer ...`, on every bind")
    return (load_token(knobs.machine_token_file,
                       f"{host.path} {MACHINE_TOKEN_KEY}"),)


def listen_address(text):
    try:
        return parse_address(text)
    except ValueError:
        raise SystemExit(f"[holo2] holo mcp --http takes {ADDRESS_SHAPE},"
                         f" got {text!r}") from None


async def follow_code(server, watch, interval, out, serving):
    while not server.started:
        await asyncio.sleep(0.05)
    print(serving, file=out, flush=True)
    while not server.should_exit:
        await asyncio.sleep(interval)
        try:
            watch()
        except Moved as moved:
            print(f"[holo2] the factory checkout moved to {moved}; holo mcp"
                  " --http exits for the new code", file=out, flush=True)
            server.should_exit = True


async def run(server, watch, interval, out, serving):
    follower = asyncio.ensure_future(
        follow_code(server, watch, interval, out, serving))
    try:
        await server.serve()
    finally:
        follower.cancel()


def serve_http(version, address, out=None, interval=CODE_CHECK_SEC):
    out = out or sys.stdout
    try:
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError as missing:
        return sdk_missing(missing)
    host = Host.locate()
    try:
        knobs = settings(host)
    except HostError as bad:
        raise SystemExit(str(bad)) from None
    tokens = machine_tokens(host, knobs)
    bound, port = listen_address(address)
    os.environ[TRANSPORT] = LOCAL
    app = build(version, TOOLS if knobs.actions else READS).streamable_http_app(
        streamable_http_path=PATH, json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False))
    server = uvicorn.Server(uvicorn.Config(
        guarded(app, tokens), host=bound, port=port, log_level="warning",
        access_log=False, lifespan="on", ws="none",
        timeout_graceful_shutdown=DRAIN_SEC))
    watch = CodeWatch(interval, out, factory_revision)
    tools = "with writes" if knobs.actions else "reads only"
    serving = (f"[holo2] holo mcp serving http://{bound}:{port}{PATH} {tools},"
               " behind the machine token")
    asyncio.run(run(server, watch, interval, out, serving))
    return 0

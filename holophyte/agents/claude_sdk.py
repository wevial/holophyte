"""One critic turn over a Claude Agent SDK session, stopped at its timeout."""
import asyncio
import os
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path

from holophyte.agents.agent_output import AgentOutput, claude_usage, count
from holophyte.config.reader import IMPL_EFFORT, IMPL_MODEL
from holophyte.redact import register_values

READ_ONLY_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit"]
SETTING_SOURCES = ["user", "project", "local"]
STOP_GRACE = 3
STDERR_TAIL_LINES = 5
SKIP_VERSION_CHECK = "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"


class LaunchError(RuntimeError):
    pass


def critic_turn(seat, goal, cwd, timeout):
    code, reply, usage = run_turn(seat, goal, cwd, timeout)
    output = AgentOutput(reply.strip(), seat.named(seat.turn(goal)),
                         exit_code=code)
    output.usage = usage
    return output


def run_turn(seat, goal, cwd, timeout):
    try:
        import claude_agent_sdk as sdk
    except ImportError as missing:
        raise LaunchError(
            f"the claude_agent_sdk package is not importable ({missing}); "
            f"install claude-agent-sdk") from None
    stderr = []
    command = seat.named(seat.turn(goal))
    try:
        options = sdk.ClaudeAgentOptions(
            cli_path=cli_path(seat.binary), cwd=str(cwd),
            model=seat.options.get("model", IMPL_MODEL),
            effort=seat.options.get("effort", IMPL_EFFORT),
            disallowed_tools=list(READ_ONLY_TOOLS),
            setting_sources=list(SETTING_SOURCES),
            system_prompt={"type": "preset", "preset": "claude_code"},
            env=credential_env(seat.options), stderr=stderr.append)
        os.environ[SKIP_VERSION_CHECK] = "1"
        result = asyncio.run(bounded(sdk, options, goal, timeout, command))
    except sdk.CLINotFoundError as error:
        raise LaunchError(f"claude binary not found: {error}") from error
    except (LaunchError, subprocess.TimeoutExpired):
        raise
    except Exception as error:
        cause = (str(error).splitlines() or [type(error).__name__])[0]
        tail = " | ".join(stderr[-STDERR_TAIL_LINES:])
        raise LaunchError(f"the claude CLI failed ({cause})"
                          + (f": {tail}" if tail else "")) from error
    usage = claude_usage({"usage": result.usage,
                          "total_cost_usd": result.total_cost_usd})
    return (1 if result.is_error else 0, result.result or "",
            dict(usage, num_turns=count(result.num_turns)))


def cli_path(binary):
    found = binary if os.path.isabs(binary) else shutil.which(binary)
    if found is None:
        raise LaunchError(f"claude binary not found: no {binary!r} on PATH")
    return found


def credential_env(options):
    key_file = options.get("api_key_file")
    if key_file is None:
        return {}
    path = Path(key_file).expanduser()
    try:
        key = path.read_text().strip()
    except FileNotFoundError:
        raise LaunchError(f"api_key_file {path} does not exist") from None
    except (OSError, UnicodeError) as error:
        raise LaunchError(f"api_key_file {path} is unreadable ({error})") from None
    if not key:
        raise LaunchError(f"api_key_file {path} is empty")
    register_values([key])
    env = {"ANTHROPIC_API_KEY": key}
    if options.get("base_url") is not None:
        env["ANTHROPIC_BASE_URL"] = options["base_url"]
    return env


async def bounded(sdk, options, goal, timeout, command):
    client = sdk.ClaudeSDKClient(options)
    exchange = asyncio.ensure_future(converse(sdk, client, goal))
    done, _ = await asyncio.wait({exchange}, timeout=timeout)
    if done:
        return exchange.result()
    process = getattr(getattr(client, "_transport", None), "_process", None)
    exchange.cancel()
    await asyncio.wait({exchange}, timeout=STOP_GRACE)
    if process is not None and process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), STOP_GRACE)
    raise subprocess.TimeoutExpired(command, timeout)


async def converse(sdk, client, goal):
    try:
        await client.connect()
        await client.query(goal)
        async for message in client.receive_response():
            if isinstance(message, sdk.ResultMessage):
                return message
        raise sdk.ClaudeSDKError("the session ended with no result")
    finally:
        await client.disconnect()

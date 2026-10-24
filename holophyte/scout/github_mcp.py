"""The scout's GitHub reads served over stdio on the MCP SDK's low-level server."""
import argparse
import os
import sys
from pathlib import Path

from holophyte.config.project import Project
from holophyte.pr.github import TOKEN_VARS
from holophyte.scout.github_reads import (
    FAILED,
    REFUSED,
    Answer,
    github_get,
    github_tests,
    origin_repo,
    record_call,
)
from holophyte.scout.scout_config import scout_config

NAME = "holo_github"
MODULE = "holophyte.scout.github_mcp"
ROOT = Path(__file__).resolve().parents[2]
NO_ORIGIN = "no GitHub origin; the GitHub tools are off"
TOOLS_SENTENCE = (
    "Two MCP tools read this project's own GitHub repository: github_get "
    "answers a GET of an allowed REST path under repos/OWNER/NAME/ (CI runs, "
    "jobs and timings, run artifacts, pull requests, issues, commit statuses "
    "and file contents), and github_tests answers a workflow run's per-test "
    "results, slowest first, from its JUnit and Playwright report "
    "artifacts. ")
GET_SCHEMA = {
    "type": "object", "required": ["path"], "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "description":
                 "A REST path under repos/OWNER/NAME/, with an optional "
                 "query string."},
        "method": {"type": "string", "description": "GET, the only method."}}}
TESTS_SCHEMA = {
    "type": "object", "required": ["run"], "additionalProperties": False,
    "properties": {
        "run": {"type": "integer", "description": "A workflow run id."},
        "artifact": {"type": "string", "description":
                     "One artifact's name; every artifact when absent."}}}
TOOLS = {
    "github_get": ("Read one allowed GitHub REST path of this repository.",
                   GET_SCHEMA),
    "github_tests": ("Read a workflow run's per-test results from its report "
                     "artifacts.", TESTS_SCHEMA),
}


def scout_server(conn, project, row):
    from store.scouts import record_scout_event

    if not scout_config(project).github:
        return None
    if origin_repo(project) is None:
        record_scout_event(conn, row.id, "scout_github", NO_ORIGIN)
        return None
    env = {"PYTHONPATH": str(ROOT)}
    if os.environ.get("HOLOPHYTE_HOME"):
        env["HOLOPHYTE_HOME"] = os.environ["HOLOPHYTE_HOME"]
    return {"name": NAME, "command": sys.executable,
            "args": ["-m", MODULE, "--project", str(project.path),
                     "--scout", str(row.id)],
            "env": env, "forward": [*TOKEN_VARS, "HOME"]}


def answered(project, repo, name, arguments):
    import jsonschema
    try:
        jsonschema.validate(arguments, TOOLS[name][1])
    except jsonschema.ValidationError as bad:
        return Answer(f"[holo2] {name} refused: {bad.message}", REFUSED)
    if repo is None:
        return Answer(f"[holo2] {name}: {NO_ORIGIN}", FAILED)
    if name == "github_get":
        return github_get(project, repo, arguments["path"],
                          arguments.get("method", "GET"))
    return github_tests(project, repo, arguments["run"],
                        arguments.get("artifact"))


def called(project, scout_id, name, arguments):
    repo = origin_repo(project)
    path = str(arguments.get("path", ""))
    if name == "github_tests" and repo is not None:
        path = (f"repos/{repo.owner}/{repo.name}/actions/runs/"
                f"{arguments.get('run')}/artifacts")
    try:
        answer = answered(project, repo, name, arguments)
    except Exception as failure:
        answer = Answer(f"[holo2] {name} failed: {failure}", FAILED)
    record_call(project, scout_id, name, path, answer)
    return answer


def build(project, scout_id):
    import anyio
    import mcp_types as types
    from mcp.server import Server
    from mcp.shared.exceptions import MCPError

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=name, description=description,
                       input_schema=schema,
                       annotations=types.ToolAnnotations(
                           read_only_hint=True, destructive_hint=False))
            for name, (description, schema) in TOOLS.items()])

    async def call_tool(ctx, params):
        if params.name not in TOOLS:
            raise MCPError(code=types.INVALID_PARAMS,
                           message=f"unknown tool {params.name!r}; the tools"
                                   f" are {', '.join(TOOLS)}")
        answer = await anyio.to_thread.run_sync(
            called, project, scout_id, params.name, params.arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=answer.text)],
            is_error=answer.error)

    return Server(NAME, on_list_tools=list_tools, on_call_tool=call_tool)


def main(argv=None):
    parser = argparse.ArgumentParser(prog=f"python3 -m {MODULE}")
    parser.add_argument("--project", required=True)
    parser.add_argument("--scout", required=True, type=int)
    args = parser.parse_args(argv)
    import anyio
    from mcp.server.stdio import stdio_server

    project = Project.locate(Path(args.project), adopt=False)
    server = build(project, args.scout)

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

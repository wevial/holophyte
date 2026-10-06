"""`holo mcp`: the tool table served over stdio on the MCP SDK's low-level server."""
import sys

from holophyte.holo.mcp_tools import (
    READ,
    TOOLS,
    Answer,
    blank,
    input_schema,
    run_tool,
)

NAME = "holo"
PACKAGE = "mcp"
INSTALL = "python3 -m pip install --user -r requirements.txt"


def described(types, tool):
    return types.Tool(
        name=tool.name, description=tool.description,
        input_schema=input_schema(tool),
        annotations=types.ToolAnnotations(read_only_hint=tool.tier == READ,
                                          destructive_hint=False))


def refusal(tool, arguments):
    import jsonschema
    empty = blank(tool, arguments)
    if empty is not None:
        return f"{empty} must be non-blank text"
    try:
        jsonschema.validate(arguments, input_schema(tool))
    except jsonschema.ValidationError as bad:
        return bad.message
    return None


def build(version):
    import anyio
    import mcp_types as types
    from mcp.server import Server
    from mcp.shared.exceptions import MCPError

    named = {tool.name: tool for tool in TOOLS}

    async def list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[described(types, tool) for tool in TOOLS])

    async def call_tool(ctx, params):
        tool = named.get(params.name)
        if tool is None:
            raise MCPError(code=types.INVALID_PARAMS,
                           message=f"unknown tool {params.name!r}; the tools"
                                   f" are {', '.join(named)}")
        arguments = params.arguments or {}
        refused = refusal(tool, arguments)
        if refused is not None:
            answer = Answer(True, f"[holo2] {tool.name}: {refused}")
        else:
            answer = await anyio.to_thread.run_sync(run_tool, tool, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=answer.text)],
            structured_content=answer.body, is_error=answer.error)

    return Server(NAME, version=version, on_list_tools=list_tools,
                  on_call_tool=call_tool)


def serve(version):
    try:
        import anyio
        from mcp.server.stdio import stdio_server
    except ImportError as missing:
        print(f"[holo2] holo mcp needs the MCP Python SDK, the package"
              f" {PACKAGE!r}, which does not import ({missing}); install it"
              f" with {INSTALL}", file=sys.stderr)
        return 1
    server = build(version)

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)
    return 0

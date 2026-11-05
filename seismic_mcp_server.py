import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import earthquakes


TOOL_DEFINITIONS = json.loads(Path(__file__).with_name("tools.json").read_text(encoding="utf-8"))
EVENT_FIELDS = {
    "id": {"type": "string"},
    "magnitude": {"type": "number"},
    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
}
EVENT_SCHEMA = {
    "type": "object",
    "properties": EVENT_FIELDS,
    "required": list(EVENT_FIELDS),
}
TOOL_DEFINITIONS["match_events_to_assets"]["inputSchema"]["properties"]["events"]["items"] = EVENT_SCHEMA
TOOL_DEFINITIONS["compute_parametric_payout"]["inputSchema"]["properties"]["event"].update(EVENT_SCHEMA)
VALIDATORS = {
    name: Draft202012Validator(tool["inputSchema"]) for name, tool in TOOL_DEFINITIONS.items()
}


def rpc_error(code, message):
    return McpError(types.ErrorData(code=code, message=message))


def create_app(upstream_transport=None):
    @asynccontextmanager
    async def upstream_client(_server):
        async with httpx.AsyncClient(timeout=15, transport=upstream_transport) as client:
            yield client

    server = Server("seismic-mcp-server", version="0.1.0", lifespan=upstream_client)

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=name, **tool) for name, tool in TOOL_DEFINITIONS.items()]

    async def call_tool(request):
        name = request.params.name
        if name not in VALIDATORS:
            raise rpc_error(-32602, f"Unknown tool: {name}")
        arguments = request.params.arguments or {}
        error = next(VALIDATORS[name].iter_errors(arguments), None)
        if error is not None:
            raise rpc_error(-32602, f"{error.json_path}: {error.message}")
        handler = getattr(earthquakes, name)
        try:
            # Reject non-finite JSON numbers before any upstream call or calculation.
            json.dumps(arguments, allow_nan=False)
            result = await handler(server.request_context.lifespan_context, **arguments)
            content = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ValueError, OverflowError) as exc:
            raise rpc_error(-32602, str(exc)) from exc
        except earthquakes.UpstreamError as exc:
            raise rpc_error(-32000, str(exc)) from exc
        return types.ServerResult(types.CallToolResult(
            structuredContent=result,
            content=[types.TextContent(type="text", text=content)],
        ))

    # Register directly because the SDK decorator converts tool errors into isError results.
    server.request_handlers[types.CallToolRequest] = call_tool

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=os.getenv("MCP_ALLOWED_HOSTS", "localhost,localhost:*,127.0.0.1,127.0.0.1:*").split(","),
        allowed_origins=os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if os.getenv("MCP_ALLOWED_ORIGINS") else [],
    )
    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True, security_settings=security)

    @asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    async def health(_request):
        return JSONResponse({"status": "ok"})

    class McpEndpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    return Starlette(
        routes=[Route("/healthz", health), Route("/mcp", McpEndpoint())],
        lifespan=lifespan,
    )


app = create_app()

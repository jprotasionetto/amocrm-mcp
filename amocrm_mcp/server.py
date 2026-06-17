"""MCP server entrypoint: compose Config + AuthManager + AmoClient + FastMCP (FR-24, FR-29, FR-30).
Runtime composition:
1. main() loads Config (env vars via Pydantic BaseSettings)
2. Initializes AuthManager (loads persisted tokens or env fallback)
3. Creates AmoClient with AuthManager + RateLimitedTransport
4. Creates FastMCP instance with AmoClient as context dependency
5. Imports src/tools/__init__.py triggering @mcp.tool() decorator registration
6. Logs tool count registered
7. Runs FastMCP with configured transport (stdio default, http via config)
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable

from fastmcp import FastMCP

from amocrm_mcp.auth import AuthError, RefreshTokenExpiredError
from amocrm_mcp.client import AmoAPIError, AmoClient, error_response

logger = logging.getLogger("amocrm_mcp.server")

EXPECTED_TOOL_COUNT = 36

mcp = FastMCP("amoCRM MCP Server")

_client: AmoClient | None = None


async def execute_tool(fn: Callable[..., dict], *args: Any, **kwargs: Any) -> dict:
    """Shared wrapper that invokes a tool function with the AmoClient instance.
    Catches typed client exceptions and converts them into FR-23 error envelopes.
    All tool handlers delegate here for consistent error handling.
    """
    if _client is None:
        return error_response(
            "Server not initialized",
            500,
            "AmoClient has not been created. Server startup may have failed.",
        )
    try:
        return await fn(_client, *args, **kwargs)
    except AmoAPIError as exc:
        return error_response(exc.message, exc.status_code, exc.detail)
    except RefreshTokenExpiredError as exc:
        return error_response(
            "Refresh token expired",
            401,
            str(exc),
        )
    except AuthError as exc:
        return error_response(
            "Authentication error",
            401,
            str(exc),
        )


def main() -> None:
    """Compose runtime and start the MCP server."""
    import asyncio

    asyncio.run(_async_main())


def _get_tool_count() -> int:
    """Get the number of registered tools in a version-compatible way."""
    # FastMCP 2+/3+ stores tools in _tool_manager
    if hasattr(mcp, '_tool_manager') and hasattr(mcp._tool_manager, 'tools'):
        return len(mcp._tool_manager.tools)
    # FastMCP 1.x - fallback via _tools dict
    if hasattr(mcp, '_tools'):
        return len(mcp._tools)
    # If we can't determine, return expected count to skip validation
    logger.warning("Cannot determine tool count - skipping validation")
    return EXPECTED_TOOL_COUNT


async def _async_main() -> None:
    """Async composition and server startup."""
    global _client

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    from amocrm_mcp.config import Config

    config = Config()
    logger.info("Configuration loaded for subdomain: %s", config.subdomain)

    from amocrm_mcp.auth import AuthManager

    auth = AuthManager(config)
    logger.info("AuthManager initialized")

    _client = AmoClient(auth=auth, base_url=config.base_url)
    logger.info("AmoClient created with base_url: %s", config.base_url)

    import amocrm_mcp.tools  # noqa: F401 -- triggers @mcp.tool() registration

    tool_count = _get_tool_count()
    if tool_count != EXPECTED_TOOL_COUNT:
        logger.warning(
            "Expected %d tools registered, got %d.",
            EXPECTED_TOOL_COUNT,
            tool_count,
        )
    else:
        logger.info(
            "amoCRM MCP server started with %d tools on %s transport",
            tool_count,
            config.transport,
        )

    try:
        if config.transport in ("sse", "http"):
            # FastMCP 3+: Streamable HTTP transport (endpoint: /mcp)
            # SSE legacy is deprecated - use http transport for Claude.ai compatibility
            await mcp.run_async(
                transport="http",
                host="0.0.0.0",
                port=config.port,
            )
        else:
            await mcp.run_async()
    finally:
        if _client is not None:
            await _client.close()

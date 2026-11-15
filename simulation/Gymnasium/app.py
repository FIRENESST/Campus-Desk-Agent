from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from mcp.server.mcpserver.exceptions import ToolError

from mcp_service import create_mcp_server
from store import GymError, GymStore, load_venues


STATIC_DIR = Path(__file__).resolve().parent / "static"
ERROR_STATUSES = {
    "INVALID_ARGUMENT": 400,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "CAPACITY_FULL": 409,
    "SLOT_UNAVAILABLE": 409,
}


class ReservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue_id: Any = None
    user_name: Any = None
    slot_id: Any = None


class AgentCallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any]


def create_app(store: GymStore | None = None) -> FastAPI:
    managed_store = store is None
    store = store if store is not None else GymStore(
        STATIC_DIR.parent / ".data" / "reservations.sqlite3",
        venues=load_venues(os.getenv("GYM_VENUES_FILE")),
    )
    mcp = create_mcp_server(store)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if managed_store:
                store.close()

    app = FastAPI(
        title="GYM / 场地预约管理",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.mcp = mcp

    @app.exception_handler(GymError)
    async def handle_gym_error(request: Request, error: GymError) -> JSONResponse:
        content = {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        }
        return JSONResponse(
            status_code=ERROR_STATUSES[error.code],
            content={"error": content} if request.url.path == "/agent/call" else content,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        content = {
            "code": "INVALID_ARGUMENT",
            "message": "请求参数格式不正确",
            "retryable": False,
        }
        return JSONResponse(
            status_code=400,
            content={"error": content} if request.url.path == "/agent/call" else content,
        )

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/admin", include_in_schema=False)
    def management_console() -> FileResponse:
        return FileResponse(STATIC_DIR / "admin.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/venues")
    def list_venues() -> dict[str, Any]:
        return {"venues": store.list_venues(), "booking_window": store.booking_window()}

    @app.get("/api/slots")
    def list_slots(venue_id: str, date: str) -> dict[str, Any]:
        return {"slots": store.list_slots(venue_id, date)}

    @app.get("/api/reservations")
    def list_reservations() -> dict[str, Any]:
        return {"reservations": store.list_reservations()}

    @app.post("/api/reservations", status_code=201)
    def reserve_venue(payload: ReservationInput) -> dict[str, Any]:
        return store.reserve(payload.venue_id, payload.user_name, payload.slot_id)

    @app.delete("/api/reservations/{reservation_id}")
    def cancel_reservation(reservation_id: str) -> dict[str, Any]:
        reservation = store.cancel(reservation_id)
        return {"cancelled": True, "reservation": reservation}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "gymnasium"}

    @app.get("/agent/tools")
    async def list_agent_tools() -> dict[str, Any]:
        tools = await mcp.list_tools()
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                }
                for tool in tools
            ]
        }

    @app.post("/agent/call")
    async def call_agent_tool(payload: AgentCallInput) -> dict[str, str]:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        tool = tools.get(payload.name)
        if tool is None:
            raise GymError("NOT_FOUND", f"未知工具：{payload.name}")
        allowed = tool.input_schema.get("properties", {})
        if any(key not in allowed for key in payload.arguments):
            raise GymError(
                "INVALID_ARGUMENT", f"只允许参数：{'、'.join(allowed) or '无'}"
            )
        try:
            result = await mcp.call_tool(payload.name, payload.arguments)
        except ToolError as error:
            raise GymError("INVALID_ARGUMENT", str(error)) from error
        if result.is_error:
            error = result.structured_content
            raise GymError(
                error["code"], error["message"], retryable=error["retryable"]
            )
        return {"content": result.content[0].text}

    app.mount("/", mcp_app)
    return app


app = create_app()

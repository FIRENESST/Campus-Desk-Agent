from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from store import GymError, GymStore


def _result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        structured_content=payload,
        is_error=is_error,
    )


def _error_result(error: GymError) -> CallToolResult:
    return _result(
        {"code": error.code, "message": error.message, "retryable": error.retryable},
        is_error=True,
    )


def create_mcp_server(store: GymStore) -> MCPServer:
    mcp = MCPServer(
        "gymnasium",
        title="健身房场地预约",
        instructions="查询场地容量与剩余名额，为用户预约场地或按预约编号取消预约。",
    )

    @mcp.tool(structured_output=False)
    def gym_list_venues() -> CallToolResult:
        """查询全部健身房场地、容量、已预约人数和剩余名额。无需参数。"""
        return _result({"venues": store.list_venues()})

    @mcp.tool(structured_output=False)
    def gym_reserve_venue(venue_id: str, user_name: str, slot_id: str | None = None) -> CallToolResult:
        """预约场馆。校园应用必须提供来自场馆时段目录的 slot_id；user_name 由业务服务传入。"""
        try:
            return _result(store.reserve(venue_id, user_name, slot_id))
        except GymError as error:
            return _error_result(error)

    @mcp.tool(structured_output=False)
    def gym_cancel_reservation(reservation_id: str) -> CallToolResult:
        """按 reservation_id 取消已有预约并释放一个场地名额。"""
        try:
            reservation = store.cancel(reservation_id)
            return _result({"cancelled": True, "reservation": reservation})
        except GymError as error:
            return _error_result(error)

    return mcp

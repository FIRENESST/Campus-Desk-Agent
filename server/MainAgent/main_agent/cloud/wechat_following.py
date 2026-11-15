"""Per-account preferences for the campus WeChat article list."""

import os

import httpx2
from psycopg.errors import UndefinedTable
from pydantic import BaseModel, ConfigDict, Field

from ..campus_cluster.subskills.campus_digest import digest_request
from .errors import ServiceError


class WeChatFollowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    followed: bool


async def _digest(path, **kwargs):
    try:
        return await digest_request(path, **kwargs)
    except httpx2.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            raise ServiceError(422, "invalid_wechat_query", "公众号筛选条件无效。") from None
    except (httpx2.HTTPError, httpx2.InvalidURL, ValueError, RecursionError):
        pass
    raise ServiceError(503, "wechat_unavailable", "暂时无法读取公众号总成，请稍后再试。")


def _execute_preference(conn, query, params):
    try:
        return conn.execute(query, params)
    except UndefinedTable:
        raise ServiceError(503, "wechat_following_unavailable", "特别关注需完成数据库迁移。") from None


async def following_feed(db, user_id, params):
    if len(params.multi_items()) != len(params) or set(params) - {"account", "title", "offset"}:
        raise ServiceError(422, "invalid_wechat_query", "公众号筛选条件无效。")
    enabled = True
    try:
        following = await db.run(lambda conn: [row["account_id"] for row in _execute_preference(conn,
            "SELECT account_id FROM user_wechat_following WHERE user_id=%s ORDER BY account_id", (user_id,),
        )])
    except ServiceError as exc:
        if exc.code != "wechat_following_unavailable":
            raise
        following = []
        enabled = False
    if following:
        key = os.getenv("MPM_RUNTIME_STATUS_KEY")
        if not key:
            raise ServiceError(503, "wechat_unavailable", "暂时无法读取特别关注，请通过主管理器启动服务。")
        feed = await _digest("/internal/wechat/following", params=dict(params), runtime_key=key,
                             payload={"following": following})
    else:
        feed = await _digest("/api/campus-desk/wechat", params=dict(params))
    return {**feed, "following": following, "following_enabled": enabled}


async def change_wechat_follow(db, user_id, data):
    if data.followed:
        response = await _digest("/api/campus-desk/wechat/accounts", params={"id": data.account_id, "limit": 1})
        if not any(account.get("id") == data.account_id for account in response.get("accounts", ())):
            raise ServiceError(404, "wechat_account_not_found", "公众号暂未收录，请刷新名录。")
        query = "INSERT INTO user_wechat_following(user_id,account_id) VALUES (%s,%s) ON CONFLICT DO NOTHING"
    else:
        query = "DELETE FROM user_wechat_following WHERE user_id=%s AND account_id=%s"
    await db.run(lambda conn: _execute_preference(conn, query, (user_id, data.account_id)))
    return {"account_id": data.account_id, "followed": data.followed}

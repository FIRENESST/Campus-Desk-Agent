"""Compose the delivered application with the campus website's private routes."""

import hmac
import re
from typing import Annotated

from .student import student_feed, verified_claim


def wrap(app, *, gateway_key):
    from fastapi import HTTPException, Request
    from pydantic import BaseModel, ConfigDict, StringConstraints
    from starlette.datastructures import Headers
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse

    from app.core import storage
    from app.core.security import Boundary, Principal, UPLOAD_PATHS

    store_tip = getattr(storage, "store_tip", None)

    class Tip(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        content: Annotated[str, StringConstraints(min_length=5, max_length=2000)]

    def tip(payload: Tip):
        if store_tip is None:
            # Delivery 1.2 stores text-only tips directly in the database.
            from app.core import db

            tid = db.add_tip(payload.content)
        else:
            tid = store_tip(payload.content, "", None, "campus-desk")
        return {"ok": True, "tid": tid}

    def reviewed(request: Request, key: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]):
        if not request.state.principal.can_write:
            raise HTTPException(403, "需要操作员权限")
        record = verified_claim(key)
        if record is None:
            raise HTTPException(404, "没有可复用的核验记录")
        return record

    app.add_api_route("/internal/campus-desk/rumors", student_feed, methods=["GET"], include_in_schema=False)
    app.add_api_route("/internal/campus-desk/tips", tip, methods=["POST"], status_code=201, include_in_schema=False)
    app.add_api_route("/internal/rumor-review/{key}", reviewed, methods=["GET"], include_in_schema=False)
    student_routes = {("/internal/campus-desk/rumors", "GET"), ("/internal/campus-desk/tips", "POST")}

    class CampusDeskBoundary(Boundary):
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await super().__call__(scope, receive, send)
            path, method = scope["path"], scope["method"]
            headers = Headers(scope=scope)
            bridge = headers.getlist("x-campus-rumor-key")
            audience = headers.get("x-campus-rumor-audience")
            peer = scope.get("client")
            valid = (gateway_key and peer and peer[0] == "127.0.0.1" and len(bridge) == 1
                     and hmac.compare_digest(gateway_key.encode(), bridge[0].encode()))
            if path.startswith("/internal/campus-desk/"):
                if not valid or audience != "student" or (path, method) not in student_routes:
                    return await JSONResponse({"detail": "校园应用接口需要本地授权"}, status_code=403)(scope, receive, send)
                return await self.normal(scope, receive, send)
            if (valid and audience == "admin" and not path.startswith("/internal/")
                    and path not in {"/login", "/logout", "/api/assess"}):
                actor = headers.get("x-campus-rumor-actor", "")
                nonce = headers.get("x-campus-rumor-nonce", "")
                if (not re.fullmatch(r"campus-desk:[0-9a-fA-F-]{36}", actor)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{24}", nonce)
                        or (method != "GET" and headers.getlist("x-campus-request") != ["1"])):
                    return await JSONResponse({"detail": "请求未获授权"}, status_code=403)(scope, receive, send)
                scope["state"] = {**scope.get("state", {}), "principal": Principal(actor, "admin"),
                                  "session_login": False, "csp_nonce": nonce}
                handler = self.upload if path in UPLOAD_PATHS else self.normal
                return await handler(scope, receive, send)
            return await super().__call__(scope, receive, send)

    if not any(middleware.cls is Boundary for middleware in app.user_middleware):
        raise RuntimeError("不兼容的校园谣言防控交付版，缺少原有鉴权中间件")
    # Reuse the delivered body limits and keep FastAPI's request and lifespan middleware.
    app.user_middleware = [Middleware(CampusDeskBoundary) if middleware.cls is Boundary else middleware
                           for middleware in app.user_middleware]
    return app

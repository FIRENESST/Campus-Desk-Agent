"""Run the project-owned identity provider on the supervisor's loopback port."""

import json
import os
from pathlib import Path

import uvicorn
from dotenv import dotenv_values

from .app import create_app


def main():
    public_url = os.getenv("CAS_SITE_ORIGIN") or os.getenv("CLOUD_PUBLIC_URL")
    if not public_url and (website_config := os.getenv("CAS_WEBSITE_ENV_FILE")):
        public_url = dotenv_values(website_config, encoding="utf-8-sig").get("CLOUD_PUBLIC_URL")
    state = os.getenv("CAS_DATA_DIR")
    if not state:
        raise SystemExit("请通过本地主管理器启动 cas-server")
    data = Path(state)
    uvicorn.run(create_app(data, public_url or "http://127.0.0.1:8082", identity_key=os.getenv("MPM_RUNTIME_STATUS_KEY", ""),
                           public_sites=json.loads(os.getenv("CAS_PUBLIC_SITES", "{}"))),
                host="127.0.0.1", port=8193,
                access_log=False, proxy_headers=False, limit_concurrency=64)


if __name__ == "__main__":
    main()

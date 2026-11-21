"""Launch the delivered campus rumor service in its own Python environment."""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="运行受主管理器管理的校园谣言防控服务器")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not (directory / "app" / "main.py").is_file():
        parser.error(f"交付包缺少 app/main.py：{directory}。请检查 CAMPUS_RUMOR_DIR。")
    os.chdir(directory)
    sys.path[:0] = [str(directory), str(Path(__file__).resolve().parents[2])]
    os.environ["HOST"] = "127.0.0.1"
    os.environ["PORT"] = os.environ.get("CAMPUS_RUMOR_PORT") or "8100"
    os.environ["LLM_MODE"] = "rules"
    os.environ.setdefault("CAMPUS_ENABLE_XHS", "false")
    from app.core.config import ENABLE_SCHEDULE, ENABLE_XHS, RUNTIME_DIR, YSXY_RAW_DIR, integer
    from app.main import app, create_server

    from server.CampusRumor.collection import start_collection_with_app
    from server.CampusRumor.integration import wrap

    interval = integer("CAMPUS_INGEST_INTERVAL_MINUTES", 5, 1, 1440) * 60 if ENABLE_SCHEDULE else None
    xhs_query = os.getenv("CAMPUS_XHS_QUERY", "").strip()[:100]
    start_collection_with_app(app, directory, YSXY_RAW_DIR,
                              pages=integer("CAMPUS_RUMOR_COLLECT_PAGES", 2, 1, 200), interval=interval,
                              xhs_enabled=ENABLE_XHS and bool(xhs_query), xhs_profile=RUNTIME_DIR / "xhs-profile" / "Default",
                              xhs_query=xhs_query)
    server = create_server()
    server.config.app = wrap(app, gateway_key=os.environ.get("MPM_RUMOR_BRIDGE_KEY", ""))
    server.run()
    if not server.started:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

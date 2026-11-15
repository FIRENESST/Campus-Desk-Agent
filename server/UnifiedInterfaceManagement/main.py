"""Run the interface manager as an HTTP server or on a pair of local pipes."""

import argparse
import asyncio
import sys
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import urlsplit

from gateway import create_app
from local import serve_local
from settings import DEFAULT_PORT, Settings


def main():
    parser = argparse.ArgumentParser(description="统一接口管理器")
    parser.add_argument("command", choices=("serve", "local"), nargs="?", default="serve")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--trusted-proxies", help="受信代理 IP 或网段，以逗号分隔")
    parser.add_argument("--ssl-certfile", type=Path)
    parser.add_argument("--ssl-keyfile", type=Path)
    args = parser.parse_args()
    if args.env_file is not None and not args.env_file.is_file():
        parser.error("指定的配置文件不存在")
    if not 1 <= args.port <= 65_535:
        parser.error("端口需为 1 到 65535")
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("请同时指定 TLS 证书和私钥")
    if args.trusted_proxies:
        try:
            for address in args.trusted_proxies.split(","):
                network = ip_network(address.strip(), strict=False)
                if network.prefixlen == 0:
                    raise ValueError
        except ValueError:
            parser.error("受信代理需填写具体 IP 或非全网网段")
    try:
        settings = Settings.from_env(args.env_file)
    except ValueError as exc:
        parser.error(str(exc))
    app = create_app(settings)
    if args.command == "local":
        scheme = urlsplit(settings.public_url).scheme
        asyncio.run(serve_local(app, sys.stdin.buffer, sys.stdout.buffer, scheme=scheme))
        return

    import uvicorn

    uvicorn.run(
        app, host=args.host, port=args.port, access_log=False,
        proxy_headers=bool(args.trusted_proxies), forwarded_allow_ips=args.trusted_proxies,
        ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

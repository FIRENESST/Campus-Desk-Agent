"""Manage the LAN and FRP entry points for the website and CAS."""

import re
from dataclasses import dataclass, replace
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from urllib.parse import urlsplit

from .services import Service


LAN_PROXY = "external-website"
LAN_BACKEND = "external-website-app"
LAN_SERVICES = (LAN_PROXY, LAN_BACKEND)
FRP_PROXY = "external-frp"
FRP_BACKEND = "external-frp-app"
FRP_SERVICES = (FRP_PROXY, FRP_BACKEND)
LAN_NETWORKS = tuple(IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
HELP = """external status                                      查看校园助手和 CAS 的内网地址
external configure website 内网IPv4 [网站端口 [本机端口 [CAS端口]]]  保存内网配置，默认 8443、8083、8444
external start website                               启动校园助手和 CAS 的内网 HTTPS 入口
external stop website                                停止两个内网 HTTPS 入口
external logs website                                查看代理和网站日志路径
external cert website                                查看供客户端信任的根证书路径
external firewall website                            显示 Windows 防火墙放行命令
external configure frp 网站HTTPS源地址 CAS-HTTPS源地址  保存公网域名和两个本机隧道目标
external start frp                                   启动本机 FRP 转发目标
external stop frp                                    停止本机 FRP 转发目标
external status frp                                  查看公网源地址和本机隧道目标
"""


def _website_process(website, name, port, origin, cas_origin):
    return replace(
        website, name=name, start_all=False,
        env=(
            *website.env,
            ("USER_WEB_HOST", "127.0.0.1"),
            ("USER_WEB_PORT", str(port)),
            ("USER_WEB_PUBLIC_URL", origin),
            ("USER_WEB_TRUSTED_PROXY", "127.0.0.1"),
            ("USER_WEB_CAS_PUBLIC_URL", cas_origin),
        ),
    )


@dataclass(frozen=True)
class ExternalWebsite:
    address: str
    port: int = 8443
    backend_port: int = 8083
    cas_port: int | None = None

    def __post_init__(self):
        if self.cas_port is None:
            object.__setattr__(self, "cas_port", 8445 if self.port == 8444 else 8444)
        try:
            address = IPv4Address(self.address)
        except (ValueError, TypeError):
            raise ValueError("请输入本机的内网 IPv4 地址") from None
        if self.address != str(address) or not any(address in network for network in LAN_NETWORKS):
            raise ValueError("网站局域网入口需要本机的内网 IPv4 地址")
        if any(type(port) is not int or not 1 <= port <= 65535 for port in (self.port, self.backend_port, self.cas_port)):
            raise ValueError("端口须为 1–65535 的整数")
        if 80 in (self.port, self.cas_port):
            raise ValueError("80 是 Caddy 的 HTTP 端口，请指定 HTTPS 端口，例如 8443")
        if self.port == self.cas_port or self.backend_port == 8193:
            raise ValueError("校园助手与 CAS 需要独立的 HTTPS 端口及后端端口")

    @property
    def origin(self):
        port = "" if self.port == 443 else f":{self.port}"
        return f"https://{self.address}{port}"

    @property
    def cas_origin(self):
        port = "" if self.cas_port == 443 else f":{self.cas_port}"
        return f"https://{self.address}{port}"

    @staticmethod
    def certificate_path(state: Path):
        return state / "external" / "caddy" / "pki" / "authorities" / "local" / "root.crt"

    def processes(self, website: Service, root: Path, state: Path) -> dict[str, Service]:
        backend = _website_process(website, LAN_BACKEND, self.backend_port, self.origin, self.cas_origin)
        proxy = Service(
            LAN_PROXY, root / "MainProcessManager",
            ("caddy.exe", "run", "--config", "website-lan.caddyfile", "--adapter", "caddyfile"),
            (LAN_BACKEND,), listener=True, start_all=False,
            env=(
                ("MPM_LAN_ADDRESS", self.address),
                ("MPM_LAN_PORT", str(self.port)),
                ("MPM_LAN_BACKEND_PORT", str(self.backend_port)),
                ("MPM_LAN_CAS_PORT", str(self.cas_port)),
                ("MPM_LAN_CAS_ORIGIN", self.cas_origin),
                ("XDG_DATA_HOME", str(state / "external")),
                ("XDG_CONFIG_HOME", str(state / "external")),
            ),
        )
        return {LAN_BACKEND: backend, LAN_PROXY: proxy}

    def firewall_command(self):
        return (
            f'New-NetFirewallRule -Name "MPM-Campus-{self.address}-{self.port}-{self.cas_port}" '
            '-DisplayName "Campus Website and CAS LAN" -Direction Inbound -Action Allow -Protocol TCP '
            f"-LocalAddress {self.address} -LocalPort {self.port},{self.cas_port} -RemoteAddress LocalSubnet -Profile Any"
        )


def _https_origin(value):
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username is not None or url.query or url.fragment
        or url.path not in {"", "/"} or url.port == 0 or not re.fullmatch(r"[a-z0-9.-]+", url.hostname)
        or any(char.isspace() for char in value)):
        raise ValueError("请输入 HTTPS 源地址，例如 https://agent.example.com")
    port = "" if url.port in {None, 443} else f":{url.port}"
    return f"https://{url.hostname}{port}"


@dataclass(frozen=True)
class ExternalFrp:
    origin: str
    cas_origin: str
    website_port: int = 8483
    cas_port: int = 8484
    backend_port: int = 8084

    def __post_init__(self):
        object.__setattr__(self, "origin", _https_origin(self.origin))
        object.__setattr__(self, "cas_origin", _https_origin(self.cas_origin))
        if self.origin == self.cas_origin:
            raise ValueError("两个 FRP 隧道需要各自的公网源地址")
        ports = (self.website_port, self.cas_port, self.backend_port)
        if (any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or len(set(ports)) != len(ports) or 8193 in ports):
            raise ValueError("FRP 转发端口和本机网站端口须互不相同")

    def processes(self, website: Service, root: Path, state: Path) -> dict[str, Service]:
        backend = _website_process(website, FRP_BACKEND, self.backend_port, self.origin, self.cas_origin)
        proxy = Service(
            FRP_PROXY, root / "MainProcessManager",
            ("caddy.exe", "run", "--config", "website-frp.caddyfile", "--adapter", "caddyfile"),
            (FRP_BACKEND,), listener=True, start_all=False,
            env=(
                ("MPM_FRP_WEBSITE_PORT", str(self.website_port)),
                ("MPM_FRP_CAS_PORT", str(self.cas_port)),
                ("MPM_FRP_BACKEND_PORT", str(self.backend_port)),
                ("MPM_FRP_CAS_ORIGIN", self.cas_origin),
                ("MPM_FRP_CAS_HOST", urlsplit(self.cas_origin).hostname),
                ("XDG_DATA_HOME", str(state / "external-frp")),
                ("XDG_CONFIG_HOME", str(state / "external-frp")),
            ),
        )
        return {FRP_BACKEND: backend, FRP_PROXY: proxy}


def _show_frp(publication, status=""):
    print(f"website  {publication.origin}/  127.0.0.1:{publication.website_port}  {status}".rstrip())
    print(f"cas      {publication.cas_origin}/  127.0.0.1:{publication.cas_port}  {status}".rstrip())


def dispatch_external(arg, actor, supervisor):
    match arg.split():
        case [] | ["help"]:
            print(HELP)
        case ["status"] | ["status", "website"]:
            publication = supervisor.external_website
            if publication is None:
                print("website  未配置")
                return
            print(f"website  {publication.origin}/")
            print(f"cas      {publication.cas_origin}/")
            for label, name in (("HTTPS 代理", LAN_PROXY), ("网站后端", LAN_BACKEND)):
                status, pid = supervisor.status(name)[name]
                print(f"  {label}  {status}  PID {pid or '-'}")
        case ["configure", "website", address, *ports] if len(ports) <= 3:
            try:
                values = [int(port) for port in ports]
            except ValueError:
                raise ValueError("端口须为 1–65535 的整数") from None
            publication = ExternalWebsite(address, *values)
            supervisor.configure_external_website(actor, publication)
            print(f"已保存 website  {publication.origin}/")
            print(f"已保存 cas      {publication.cas_origin}/")
        case ["start", "website"]:
            publication = supervisor.require_external_website()
            status = supervisor.start_external_website(actor)
            print(f"website  {status}  {publication.origin}/")
            print(f"cas      {status}  {publication.cas_origin}/")
        case ["stop", "website"]:
            supervisor.stop_external_website(actor)
            print("website  stopped")
            print("cas      stopped")
        case ["logs", "website"]:
            supervisor.require_external_website()
            for name in LAN_SERVICES:
                print(supervisor.log_path(name))
        case ["cert", "website"]:
            supervisor.require_external_website()
            certificate = ExternalWebsite.certificate_path(supervisor.store.directory)
            if not certificate.is_file():
                raise ValueError("根证书尚未生成，请先运行 external start website")
            print(certificate)
        case ["firewall", "website"]:
            print(supervisor.require_external_website().firewall_command())
        case ["configure", "frp", website_url, cas_url]:
            publication = ExternalFrp(website_url, cas_url)
            supervisor.configure_external_frp(actor, publication)
            _show_frp(publication)
        case ["start", "frp"]:
            publication = supervisor.require_external_frp()
            _show_frp(publication, supervisor.start_external_frp(actor))
        case ["stop", "frp"]:
            supervisor.stop_external_frp(actor)
            print("frp  stopped")
        case ["status", "frp"]:
            publication = supervisor.require_external_frp()
            _show_frp(publication, supervisor.status(FRP_PROXY)[FRP_PROXY][0])
        case _:
            raise ValueError("请使用 external help 查看对外服务命令")

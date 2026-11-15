"""Shared connection to the project's local model proxy."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from dotenv import dotenv_values
from openai import AsyncOpenAI

from .llm import OpenAICompatibleLLMClient


LOCAL_ENV = Path(__file__).resolve().parents[2] / "llmServer" / ".env"


@dataclass(frozen=True)
class LLMServerSettings:
    base_url: str
    api_key: str = field(default="", repr=False)
    deepseek_json: bool = False

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.base_url)
            if url.port == 0:
                raise ValueError
        except ValueError:
            raise ValueError("MAIN_AGENT_LLM_SERVER_URL 端口无效") from None
        if (
            url.scheme not in {"http", "https"} or not url.hostname
            or url.username is not None or url.query or url.fragment
            or not url.path.rstrip("/").endswith("/v1")
            or any(char.isspace() for char in self.base_url)
        ):
            raise ValueError("MAIN_AGENT_LLM_SERVER_URL 必须是以 /v1 结尾的 HTTP/HTTPS 基础地址")
        if any(not 33 <= ord(char) <= 126 for char in self.api_key):
            raise ValueError("llmServer 本地密钥需使用不含空白的 ASCII 字符")

    @classmethod
    def from_env(cls) -> "LLMServerSettings":
        override_url = os.getenv("MAIN_AGENT_LLM_SERVER_URL", "").strip()
        override_key = os.getenv("MAIN_AGENT_LLM_SERVER_API_KEY")
        if override_url:
            return cls(override_url, (override_key or "").strip())

        local = dotenv_values(LOCAL_ENV, encoding="utf-8-sig") if LOCAL_ENV.is_file() else {}
        try:
            port = int(os.getenv("LOCAL_PORT", local.get("LOCAL_PORT") or "8000"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError("llmServer 的 LOCAL_PORT 必须是 1 到 65535 的整数") from None
        local_key = os.getenv("LOCAL_API_KEY", local.get("LOCAL_API_KEY") or "")
        key = local_key if override_key is None else override_key
        upstream = os.getenv("LLM_BASE_URL", local.get("LLM_BASE_URL") or "").strip()
        return cls(f"http://127.0.0.1:{port}/v1", key.strip(), urlsplit(upstream).hostname == "api.deepseek.com")


@asynccontextmanager
async def connect_llm_server(
    settings: LLMServerSettings | None = None,
) -> AsyncIterator[OpenAICompatibleLLMClient]:
    config = settings if settings is not None else LLMServerSettings.from_env()
    timeout = httpx2.Timeout(300.0, connect=10.0, pool=10.0)
    http_client = httpx2.AsyncClient(
        timeout=timeout, trust_env=False, follow_redirects=False,
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
    )
    async with AsyncOpenAI(
        base_url=config.base_url, api_key=config.api_key or "local", http_client=http_client,
        timeout=timeout, max_retries=0,
    ) as client:
        # llmServer treats an empty model as its configured LLM_MODEL.
        json_options = {
            "response_format": {"type": "json_object"},
            "extra_body": {"thinking": {"type": "disabled"}},
            "max_tokens": 8192,
        } if config.deepseek_json else None
        yield OpenAICompatibleLLMClient(client, model="", json_completion_options=json_options)

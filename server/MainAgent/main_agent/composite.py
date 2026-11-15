from typing import Any

from .core import AgentError, ToolClient, ToolSpec, UnavailableToolError


class CompositeToolError(AgentError):
    pass


class DuplicateToolError(CompositeToolError):
    pass


class CompositeToolClient:
    def __init__(self, clients: list[ToolClient]):
        self._clients = list(clients)
        self._routes: dict[str, ToolClient] = {}

    async def list_tools(self) -> list[ToolSpec]:
        specs = []
        routes = {}
        for client in self._clients:
            for tool in await client.list_tools():
                if tool.name in routes:
                    raise DuplicateToolError(f"多个服务提供了同名工具：{tool.name}")
                routes[tool.name] = client
                specs.append(tool)
        self._routes = routes
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        client = self._routes.get(name)
        if client is None:
            raise UnavailableToolError(f"没有已加载的工具路由：{name}")
        return await client.call_tool(name, arguments)

# 本地大模型 API 转发服务器

本地客户端 → `http://127.0.0.1:8000/v1` → 大模型服务商。

使用 Python 3.10+，通过 FastAPI 和 HTTPX 将请求转发到一个配置好的 **OpenAI 兼容 HTTP API**。真实的上游 API Key 保存在本机配置中，服务默认固定监听 `127.0.0.1:8000`。

## 安装与启动

在项目目录中执行以下 PowerShell 命令：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

已有 `.env` 时可沿用现有配置。首次配置时创建 `.env`，填写 API Key、上游基础地址和模型名。以下是一份 DeepSeek 配置示例。

```dotenv
LLM_API_KEY=你的实际APIKey
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LOCAL_PORT=8000
```

上述地址和模型来自 [DeepSeek 官方调用文档](https://api-docs.deepseek.com/)。其他服务商请使用其 OpenAI 兼容 API 的基础地址及模型名。基础地址可以包含路径前缀，例如 `https://服务商域名/api/v1`，不要填完整的 `/chat/completions` 地址。

启动：

```powershell
.\.venv\Scripts\python.exe server.py
```

保持终端运行，按 `Ctrl+C` 停止。改端口只需修改 `LOCAL_PORT` 后重启。Linux/macOS 使用 `python3 -m venv .venv` 和 `.venv/bin/python` 执行对应命令。

若 `python` 命令不存在，请先安装 Python 3.10+ 并加入 PATH，或使用已有 Python 解释器的完整路径。

在同一工作区部署时，MainAgent 使用此服务的 `LOCAL_PORT` 和 `LOCAL_API_KEY` 连接，并由此处的 `LLM_MODEL` 统一选择模型。先启动本服务，再启动默认监听 `8081` 的 MainAgent 云 API。 [MainAgent 启动说明](../MainAgent/README.md)

## 调用示例

检查本地服务（不会请求上游，也不会产生模型调用费用）：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

普通对话，省略 `model` 时使用 `.env` 中的 `LLM_MODEL`：

```powershell
$body = @{
    messages = @(@{ role = "user"; content = "你好，请介绍一下自己" })
    stream = $false
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
    -Uri http://127.0.0.1:8000/v1/chat/completions `
    -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

Python 客户端示例（HTTPX 已包含在依赖中）：

```python
import httpx

response = httpx.post(
    "http://127.0.0.1:8000/v1/chat/completions",
    json={"messages": [{"role": "user", "content": "你好"}]},
    timeout=300,
)
response.raise_for_status()
print(response.json()["choices"][0]["message"]["content"])

# 流式输出：逐行读取上游 SSE，包含 data: [DONE] 等原始事件。
with httpx.stream(
    "POST",
    "http://127.0.0.1:8000/v1/chat/completions",
    json={"messages": [{"role": "user", "content": "写一首短诗"}], "stream": True},
    timeout=300,
) as stream:
    stream.raise_for_status()
    for line in stream.iter_lines():
        if line:
            print(line)
```

在支持 OpenAI 兼容 API 的客户端软件中填写：

- Base URL：`http://127.0.0.1:8000/v1`。
- API Key：未设置 `LOCAL_API_KEY` 时，可填写 `local`；设置后填写该本地密钥。
- Model：填写服务商支持的模型名；请求中的模型优先于 `LLM_MODEL`。

## 接口与配置

| 本地接口 | 转发到上游基础地址后的路径 |
| --- | --- |
| `POST /v1/chat/completions` | `/chat/completions` |
| `POST /v1/completions` | `/completions` |
| `POST /v1/embeddings` | `/embeddings` |
| `POST /v1/responses` | `/responses` |
| `GET /v1/models` | `/models` |

这些接口是否可用取决于上游服务商。请求中的 `tools`、`temperature`、`stream`、多模态消息等参数会保留并转发；上游状态码、JSON 错误、SSE、请求 ID 和限流信息会返回给客户端。流式数据不会等待完整生成后才发送。

转发请求的对话上下文由该次请求提供。代理复用 HTTP 连接池，并禁用跨请求的上游 Cookie 存储和发送。

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `LLM_API_KEY` | 上游 API Key，必填 | 无 |
| `LLM_BASE_URL` | 上游基础地址，必填 | 无 |
| `LLM_MODEL` | 请求未指定模型时的默认模型 | 空 |
| `LOCAL_PORT` | 本地固定监听端口 | `8000` |
| `LLM_TIMEOUT` | 等待上游响应头或相邻响应块的超时秒数 | `300` |
| `LOCAL_API_KEY` | 可选的本地客户端密钥 | 空 |

`.env` 总是从 `server.py` 所在目录读取；系统环境变量优先。若设置了 `LOCAL_API_KEY`，模型接口需要请求头 `Authorization: Bearer 本地密钥`，`/health` 仍可直接访问。转发时始终改用 `LLM_API_KEY` 认证上游。

此服务支持 Bearer API Key 的 OpenAI 兼容接口。Anthropic、Gemini 等原生协议需要对应适配。服务默认仅供本机访问，没有配置浏览器跨域访问。

连接上游失败返回 `502`，等待上游响应头超时返回 `504`。如果流式响应已经开始后上游超时或断开，连接会中断；此时 HTTP 状态码已经发送，无法再改为 `504`。服务不自动重试模型请求。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用模拟上游，覆盖认证、路径、模型选择、错误和流式连接释放，无需真实 API Key。

还可运行真实本地 HTTP 测试（需要端口 `8000` 空闲），验证启动、认证和逐块流式转发；结束后自动停止测试服务：

```powershell
.\.venv\Scripts\python.exe tests\smoke_server.py
```

实现参考：[HTTPX 异步与流式请求](https://www.python-httpx.org/async/)、[FastAPI 流式响应](https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse)、[Uvicorn 监听设置](https://www.uvicorn.org/settings/)。

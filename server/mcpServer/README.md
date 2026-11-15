# MainAgent MCP 服务

服务监听本机 `127.0.0.1:8001`，MCP 地址是 `http://127.0.0.1:8001/mcp`。调用需要 MainAgent 的本地 Bearer 凭据。

## 启动

使用 `search_rag` 时先启动 RAG 查询服务。默认查询地址是 `http://127.0.0.1:8002/search`。如果资料管理窗口显示了其他地址，在本目录的 `.env` 中设置 `RAG_SEARCH_URL`。 [RAG 查询说明](../../DataBase/RAG/README.md#http-检索接口)

已有 `.venv` 和 `.env` 时直接启动本服务。

```powershell
.\.venv\Scripts\python.exe -B main.py
```

单独部署本服务时，需要把共用网页读取模块带到相同的相对路径。MCP 服务使用自身的虚拟环境加载它。 [共用网页读取模块](../MainAgent/main_agent/campus_cluster/subskills/domain_page_reader.py)

首次部署可用 `requirements.txt` 安装依赖，再在本目录生成本地凭据。以下命令会创建新的 `.env`。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "from pathlib import Path; from secrets import token_urlsafe; Path('.env').open('x', encoding='utf-8').write('MAIN_AGENT_MCP_TOKEN=' + token_urlsafe(32) + '\n')"
```

| 配置 | 用途 | 默认 |
| --- | --- | --- |
| `MAIN_AGENT_MCP_TOKEN` | MCP 服务端与 MainAgent 使用的同一份本地凭据 | 必填 |
| `RAG_SEARCH_URL` | RAG 的完整查询地址 | `http://127.0.0.1:8002/search` |
| `MCP_RAG_SCOPE` | 由部署方指定的资料范围 | `campus` |

系统环境变量优先于 `.env`。同一项目中的 MainAgent 客户端可以读取这份 `.env`。

## 子智能体能力清单

`get_subagent_capabilities` 无需参数，返回两种结构化视图。

| 字段 | 内容 |
| --- | --- |
| `agents` | 四个角色的 ID、名称、职责和注册技能。每项技能包含名称、用途、只读标记、启用状态和 `actions`。 |
| `skills` | 各项技能及其 `registered_agents`。每个登记角色包含 ID、名称和启用状态。办理动作列出各角色的必填信息与校验条件。 |

默认 MainAgent 云进程每次启动时从统一注册表生成 `server/MainAgent/.runtime/subagent-capabilities.json`。MCP 读取本机文件即可获取新清单。MainAgent 判断为校园问题后，会在选角前调用一次此工具。清单暂不可用或与当前注册表不符时，使用本次运行的实际注册表。该工具走既有的 MainAgent 本地鉴权。 [登记技能与角色](../MainAgent/main_agent/campus_cluster/README.md#添加子智能体技能)

MainAgent 在本轮内将登记清单与 MINTA 当前发布的动作及登录账号角色交叉核对。MCP 文件只保存登记数据。账号权限和具体资源校验在 MainAgent 的办理链路中执行。

```python
async with connect_main_agent_mcp() as tools:
    catalog_json = await tools.call_tool("get_subagent_capabilities", {})
```

## 检索工具

`search_rag` 接收 `query` 和可选的 `top_k`。条数默认 5，范围 1 到 50。结果包含检索范围、资料片段原文、来源、相似度和文档及片段 ID。

在 MainAgent 的主流程中调用。

```python
from main_agent import connect_main_agent_mcp


async def lookup(query: str) -> str:
    async with connect_main_agent_mcp() as tools:
        return await tools.call_tool("search_rag", {"query": query, "top_k": 5})
```

## 读取域名首页

`read_domain_page` 接收 `domain`，例如 `{"domain":"example.org"}`。返回 `domain`、最终来源 `source_url`、标题 `title`、静态正文 `text` 和截断标记 `truncated`。它与子智能体技能共用公网 HTTPS 校验和正文提取规则。 [子智能体技能说明](../MainAgent/main_agent/campus_cluster/README.md#读取域名首页)

```python
async def read_homepage(domain: str) -> str:
    async with connect_main_agent_mcp() as tools:
        return await tools.call_tool("read_domain_page", {"domain": domain})
```

## OCR

`ocr_document` 用本地 CPU 识别中英文混排的截图、照片、扫描 PDF 和多帧图片。支持 PNG、JPEG、WebP、TIFF、BMP 和 GIF。三份模型权重随 RapidOCR 安装，共约 30.3 MiB，第一次识别时按需加载。 [RapidOCR 用法与输出](https://rapidai.github.io/RapidOCRDocs/latest/install_usage/rapidocr/usage/)

在已有 MCP 虚拟环境安装更新后的依赖，再重启 MCP 服务。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

| 参数 | 用途 |
| --- | --- |
| `url` | 公开 HTTPS 图片或 PDF 直链，最大 10 MiB，每一跳都校验公网地址 |
| `data_base64` | 由调用端传入文件的 Base64 或 Base64 data URL，原文件最大 2 MiB，与 `url` 二选一 |
| `pages` | 从 1 开始的页码或帧序号，每次最多 5 页，默认识别前 5 页 |
| `detail` | `text` 返回文字，`lines` 加上行框和置信度，`words` 进一步返回可用的词框，默认 `lines` |
| `min_confidence` | 文本行置信度下限，范围 0 到 1，默认 0.5 |
| `rotation` | 顺时针旋转 0、90、180 或 270 度，默认 0，自动应用 EXIF 方向和文字行的 180 度纠正 |

返回 `format`、`source_url`、总页数 `page_count`、全文 `text` 和分页面 `pages`。每页包含校正后的像素宽高，文字框为相对于页面左上角的四点像素坐标。每页最多返回 3000 个正文字符、120 行和 160 个词框，`truncated` 标记默认页数上限或输出截断。

每个 MCP 进程同时运行一个 OCR 任务。繁忙时会返回可重试的工具错误。

```python
from main_agent import connect_main_agent_mcp


async def recognize_document(url: str) -> str:
    async with connect_main_agent_mcp() as tools:
        return await tools.call_tool("ocr_document", {"url": url, "pages": [1], "detail": "lines"})
```

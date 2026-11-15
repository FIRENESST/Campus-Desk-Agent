# Milvus RAG 知识库

本机库使用 Milvus Lite，存储目录为 `data/campus.db/`，默认集合为 `campus_chunks`。知识库初始为空。 [Milvus Lite 官方文档](https://milvus.io/docs/milvus_lite.md)

桌面程序、命令行和 Python 接口统一使用 `BAAI/bge-small-zh-v1.5`，在本机 CPU 上生成 512 维向量。集合记录固定的权重版本和维度，避免把不同向量空间的数据写入同一个集合。 [模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5) · [ONNX 权重版本](https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59)

在本目录执行以下命令。已有虚拟环境时可跳过第一行。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -B -m rag_db setup-model
.\.venv\Scripts\python.exe -B -m rag_db init
```

`setup-model` 一次性下载约 95 MB 的固定版本模型文件到 `models/`，并验证文件哈希。之后在本机导入和检索可离线完成。`init` 创建集合和索引并显示状态。空库片段数为 0，检索结果为 `[]`。

## 一起启动

双击 `start-rag.cmd`，或在本目录运行以下命令。

```powershell
.\.venv\Scripts\python.exe -B -m rag_db start
```

程序初始化数据库后启动查询服务和桌面资料管理窗口。查询优先使用本机端口 `8002`，端口不可用时自动选择一个可用端口。窗口顶部显示可复制的查询地址。需要固定端口时用 `start --port 端口号`。关闭资料管理窗口会结束这次启动的查询服务。

## 本地资料管理程序

双击 `start-rag-writer.cmd` 打开窗口。也可以在本目录运行 `.\.venv\Scripts\python.exe -B -m rag_writer`。

1. 在「添加资料」中填写资料范围，粘贴正文或一次选择多份文件，然后点击「写入 RAG」。输入新范围即可在写入时创建。粘贴正文时填写来源，导入文件时以文件名为来源。
2. 在「查看与删除」中，从下拉框选择库里已有的资料范围。切换范围会加载该范围的文档。选中一行并点击「查看正文」，可以按顺序浏览已存储的片段。
3. 选中一行并点击「删除选中版本」。核对确认框里的范围和文档 ID 后，从库中移除该版本的全部片段和向量。同名的不同版本在列表里分行显示。

文件支持 TXT、Markdown、CSV、TSV、JSON、JSONL、日志、XML、YAML、HTML、DOCX 和带文字层的 PDF。HTML 提取可见文字，DOCX 提取正文和表格文字。普通文本接受 UTF-8、带 BOM 的 UTF-16/32 和 GB18030。每份文件上限 20 MiB。正文会自动分片。

添加、查看和删除在短时本地进程中完成，每次操作结束即释放 Milvus Lite 库。资料范围默认是 `campus`。

## HTTP 检索接口

统一入口的 `POST /v1/rag/search` 提供鉴权检索，`/admin/rag/*` 提供资料管理。Milvus 保存资料正文片段和向量，用户资料与会话由 MainAgent 的 PostgreSQL 保存。 [统一接口管理器](../../server/UnifiedInterfaceManagement/README.md)

本机其他程序可向窗口显示的查询地址发送 JSON。默认端口可用时地址为 `http://127.0.0.1:8002/search`。同一地址上的 `GET /scopes` 返回 `{"scopes":[...]}`，可用来填充资料范围下拉框。需要单独启动查询服务时，在本目录运行 `.\.venv\Scripts\python.exe -B -m rag_api`。

```powershell
$body = @{ query = '图书馆怎么借书'; scope = 'campus'; top_k = 5 } | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8002/search' -Method Post -ContentType 'application/json; charset=utf-8' -Body $body
```

`query` 是检索问题。`scope` 默认为 `campus`，须与写入资料时的范围相同。`top_k` 默认为 5，支持 1 到 50。响应的 `hits` 包含正文 `text`、来源 `source`、余弦相似度 `score`、文档 ID、片段 ID 和片段序号。没有匹配时返回 `{"hits":[]}`。`GET /health` 可检查查询服务是否就绪。

查询服务预加载向量模型并缓存最近 128 个不同问题的向量。每次请求都从 Milvus 读取最新资料。HTTP 查询和桌面写入轮流持有本地库，查询等待过久时返回 HTTP 503，可稍后重试。

MainAgent 的本机路由反馈通过 `POST /routing/embeddings` 生成句向量。请求为 `{"texts":["脱敏后的文本"]}`，每批最多 32 条，每条最多 400 字符。响应包括向量空间标识 `space` 和按输入顺序排列的 `vectors`，超过模型词元上限的条目返回 `null`。接口仅接受本机请求，直接复用已加载的编码器。

## 命令行导入与检索

```powershell
.\.venv\Scripts\python.exe -B -m rag_db ingest '.\所选资料.md' --source '资料名称或原文链接'
.\.venv\Scripts\python.exe -B -m rag_db search '检索问题' --top-k 5
```

- 导入支持 UTF-8 文本和 Markdown。每片最多 800 字符，并按模型的 512 词元上限继续切分。相邻片段最多重叠 100 字符。
- 同一范围、来源和正文重复导入时复用片段主键。正文修改会产生独立版本，各版本可参与检索。
- 导入和查询使用同一个 `--scope`。默认范围为 `campus`，应用接入时由服务端选择调用者有权访问的范围。
- 结果包含正文、来源、文档 ID、片段 ID、片段序号和余弦相似度。MainAgent 接入时可将片段 ID、正文和来源映射到可信性编排的 `Evidence`。
- 若需迁移旧向量集合，使用 `--collection` 或 `RAG_COLLECTION` 指定新集合，并重新导入原始资料。

Python 调用入口为 `RagDatabase.ingest()` 和 `RagDatabase.search()`，直接调用即可使用项目模型。已有旧集合可通过原向量客户端读取和维护。Lite 的本地存储目录由单进程持有。在独立 Milvus 服务中使用时，可通过 `RAG_MILVUS_URI` 和 `RAG_MILVUS_TOKEN` 指定连接。 [Milvus Lite 的使用范围](https://milvus.io/docs/milvus_lite.md#Supported-features-and-limits)

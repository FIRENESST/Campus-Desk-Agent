"""Manage documents in the Milvus knowledge base from the desktop."""

import json
import os
import sys
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from milvus_lite.exceptions import DataDirLockedError
from pymilvus.exceptions import MilvusException

from rag_db import DEFAULT_COLLECTION, DEFAULT_URI, RagDatabase, RagError, _scope
from rag_embedding import EmbeddingError
from rag_materials import SUPPORTED_SUFFIXES, load_material
from rag_worker import RagWorkerError, run_worker


def _open_database() -> RagDatabase:
    return RagDatabase(
        os.getenv("RAG_MILVUS_URI", str(DEFAULT_URI)),
        os.getenv("RAG_COLLECTION", DEFAULT_COLLECTION),
        token=os.getenv("RAG_MILVUS_TOKEN", ""),
    )


def _write_batch(request: dict) -> list[dict]:
    scope = _scope(request["scope"])
    files = [Path(name) for name in request.get("files", ())]
    if files:
        materials = [(file.name, file) for file in files]
    else:
        source, text = request["source"].strip(), request["text"]
        if not source or not text.strip():
            raise RagError("请填写资料来源和正文")
        materials = [(source, text)]

    results = []
    with _open_database() as database:
        for source, material in materials:
            try:
                content = load_material(material) if isinstance(material, Path) else material
                result = database.ingest(content, source=source, scope=scope)
                results.append({"source": source, **result})
            except (RagError, EmbeddingError) as exc:
                results.append({"source": source, "error": str(exc)})
            except OSError:
                results.append({"source": source, "error": "文件读取失败"})
            except MilvusException as exc:
                results.append({"source": source, "error": f"Milvus 写入失败（{exc.code}）"})
    return results


def _handle_request(request: dict) -> dict:
    operation = request.get("operation", "ingest")
    if operation == "ingest":
        return {"results": _write_batch(request)}
    scope = request["scope"]
    with _open_database() as database:
        if operation == "list":
            scopes = database.list_scopes()
            if scope not in scopes:
                scope = "campus" if "campus" in scopes else next(iter(scopes), "")
            return {
                "scopes": scopes, "scope": scope,
                "documents": database.list_documents(scope=scope) if scope else [],
            }
        if operation == "view":
            return {"chunks": database.document_chunks(request["document_id"], scope=scope)}
        if operation == "delete":
            database.delete_document(request["document_id"], scope=scope)
            return {"deleted": True}
    raise RagError("资料操作无效")


def _worker_main() -> None:
    try:
        payload = _handle_request(json.load(sys.stdin))
    except RagError as exc:
        payload = {"error": str(exc)}
    except DataDirLockedError:
        payload = {"error": "本地知识库正忙，请稍后重试"}
    except MilvusException as exc:
        payload = {"error": f"Milvus 操作失败（{exc.code}），请刷新列表确认当前状态"}
    except (KeyError, TypeError, ValueError, AttributeError):
        payload = {"error": "资料操作参数有误"}
    except OSError:
        payload = {"error": "无法访问本地数据库目录"}
    print(json.dumps(payload))


def _run_worker(request: dict) -> dict:
    try:
        result = run_worker(Path(__file__), request)
    except RagWorkerError as exc:
        return {"error": str(exc)}
    expected = {"ingest": "results", "list": "documents", "view": "chunks", "delete": "deleted"}
    if "error" not in result and expected.get(request.get("operation", "ingest")) not in result:
        return {"error": "本地工作进程未返回操作状态"}
    return result


class RagWriter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RAG 资料管理")
        self.geometry("900x680")
        self.minsize(660, 550)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._pending = None
        self._closing = False
        self._files = ()
        self._listed_scope = None

        self.scope = tk.StringVar(value="campus")
        self.browse_scope = tk.StringVar()
        self.source = tk.StringVar()
        self.status = tk.StringVar(value="就绪")
        self.preview_title = tk.StringVar(value="选择一份资料查看正文")

        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="RAG 资料管理", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        collection = os.getenv("RAG_COLLECTION", DEFAULT_COLLECTION)
        ttk.Label(main, text=f"目标集合 {collection}").pack(anchor="w", pady=(2, 10))

        query_url = os.getenv("RAG_QUERY_URL")
        if query_url:
            query_row = ttk.Frame(main)
            query_row.pack(fill="x", pady=(0, 10))
            ttk.Label(query_row, text="查询地址").pack(side="left", padx=(0, 8))
            address = ttk.Entry(query_row, width=44)
            address.insert(0, query_url)
            address.configure(state="readonly")
            address.pack(side="left")

        self.sections = ttk.Notebook(main)
        self.sections.pack(fill="both", expand=True)
        add_tab = ttk.Frame(self.sections, padding=8)
        self.manage_tab = ttk.Frame(self.sections, padding=8)
        self.sections.add(add_tab, text="添加资料")
        self.sections.add(self.manage_tab, text="查看与删除")
        self.sections.enable_traversal()
        self._build_add_tab(add_tab)
        self._build_manage_tab()

        status_bar = ttk.Frame(main)
        status_bar.pack(fill="x", pady=(10, 0))
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=100)
        self.progress.pack(side="left", padx=(0, 10))
        ttk.Label(status_bar, textvariable=self.status).pack(side="left")

        self.sections.bind("<<NotebookTabChanged>>", self._section_changed)
        self._update_controls()

    def _build_add_tab(self, panel: ttk.Frame) -> None:
        scope_row = ttk.Frame(panel)
        scope_row.pack(fill="x", pady=(0, 8))
        ttk.Label(scope_row, text="资料范围").pack(side="left", padx=(0, 8))
        self.scope_entry = ttk.Entry(scope_row, textvariable=self.scope)
        self.scope_entry.pack(side="left")

        self.tabs = ttk.Notebook(panel)
        self.tabs.pack(fill="both", expand=True)
        self.paste_tab = ttk.Frame(self.tabs, padding=8)
        file_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.paste_tab, text="粘贴正文")
        self.tabs.add(file_tab, text="导入文件")
        self.tabs.enable_traversal()

        source_row = ttk.Frame(self.paste_tab)
        source_row.pack(fill="x", pady=(0, 8))
        ttk.Label(source_row, text="资料来源").pack(side="left", padx=(0, 8))
        ttk.Entry(source_row, textvariable=self.source).pack(side="left", fill="x", expand=True)
        self.text = ScrolledText(self.paste_tab, height=8, wrap="word")
        self.text.pack(fill="both", expand=True)

        ttk.Button(file_tab, text="选择文件", command=self._choose_files).pack(anchor="w")
        ttk.Label(file_tab, text="支持 TXT、Markdown、CSV、JSON、HTML、DOCX、PDF 等").pack(anchor="w", pady=6)
        self.files_box = ScrolledText(file_tab, height=7, wrap="word", state="disabled")
        self.files_box.pack(fill="both", expand=True)

        self.write_button = ttk.Button(panel, text="写入 RAG", command=self._submit)
        self.write_button.pack(anchor="w", pady=8)
        self.results_box = ScrolledText(panel, height=6, wrap="word", state="disabled")
        self.results_box.pack(fill="x")

    def _build_manage_tab(self) -> None:
        tools = ttk.Frame(self.manage_tab)
        tools.pack(fill="x", pady=(0, 8))
        ttk.Label(tools, text="资料范围").pack(side="left", padx=(0, 8))
        self.scope_select = ttk.Combobox(tools, textvariable=self.browse_scope, state="readonly", width=18)
        self.scope_select.pack(side="left", padx=(0, 8))
        self.scope_select.bind("<<ComboboxSelected>>", self._refresh_documents)
        self.refresh_button = ttk.Button(tools, text="刷新列表", command=self._refresh_documents)
        self.refresh_button.pack(side="left")
        self.view_button = ttk.Button(tools, text="查看正文", command=self._view_selected)
        self.view_button.pack(side="left", padx=8)
        self.delete_button = ttk.Button(tools, text="删除选中版本", command=self._delete_selected)
        self.delete_button.pack(side="left")

        table = ttk.Frame(self.manage_tab)
        table.pack(fill="both", expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.documents = ttk.Treeview(
            table, columns=("source", "document_id"), show="headings", selectmode="browse", height=7,
        )
        self.documents.heading("source", text="资料来源")
        self.documents.heading("document_id", text="文档 ID")
        self.documents.column("source", width=260, minwidth=160)
        self.documents.column("document_id", width=480, minwidth=400)
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.documents.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.documents.xview)
        self.documents.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.documents.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.documents.bind("<<TreeviewSelect>>", self._selection_changed)
        self.documents.bind("<Return>", lambda event: self._view_selected())
        self.documents.bind("<Double-1>", lambda event: self._view_selected() if self.documents.identify_row(event.y) else None)

        preview_bar = ttk.Frame(self.manage_tab)
        preview_bar.pack(fill="x", pady=(8, 4))
        ttk.Label(preview_bar, textvariable=self.preview_title).pack(side="left")
        self.close_preview = ttk.Button(preview_bar, text="收起预览", command=self._clear_preview, state="disabled")
        self.close_preview.pack(side="right")
        self.copy_id = ttk.Button(preview_bar, text="复制文档 ID", command=self._copy_document_id, state="disabled")
        self.copy_id.pack(side="right", padx=(0, 6))
        self.preview = ScrolledText(self.manage_tab, height=8, wrap="word", state="disabled")
        self.preview.pack(fill="both", expand=True)

    @staticmethod
    def _show(box: ScrolledText, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _selected_document(self) -> str | None:
        selection = self.documents.selection()
        if selection and self._listed_scope == self.browse_scope.get():
            return selection[0]
        return None

    def _update_controls(self) -> None:
        busy = self._pending is not None or self._closing
        for widget in (self.scope_entry, self.write_button, self.refresh_button):
            widget.state(["disabled" if busy else "!disabled"])
        can_choose_scope = not busy and bool(self.scope_select.cget("values"))
        self.scope_select.state(["!disabled" if can_choose_scope else "disabled"])
        can_manage = not busy and self._selected_document() is not None
        for button in (self.view_button, self.delete_button, self.copy_id):
            button.state(["!disabled" if can_manage else "disabled"])

    def _copy_document_id(self) -> None:
        document_id = self._selected_document()
        if document_id is None:
            return
        self.clipboard_clear()
        self.clipboard_append(document_id)
        self.status.set("文档 ID 已复制")

    def _clear_preview(self) -> None:
        self.preview_title.set("选择一份资料查看正文")
        self._show(self.preview, "")
        self.close_preview.state(["disabled"])

    def _clear_documents(self) -> None:
        self._listed_scope = None
        children = self.documents.get_children()
        if children:
            self.documents.delete(*children)
        self._clear_preview()
        self._update_controls()

    def _section_changed(self, event=None) -> None:
        if self.sections.select() == str(self.manage_tab):
            self._refresh_documents()

    def _selection_changed(self, event=None) -> None:
        self._clear_preview()
        self._update_controls()

    def _refresh_documents(self, event=None, *, notice: str = "") -> None:
        if self._pending is not None or self._closing:
            return
        prefix = f"{notice}，" if notice else ""
        self._clear_documents()
        scope = self.browse_scope.get()

        def show_documents(result: dict) -> None:
            selected = result["scope"]
            self.scope_select.configure(values=result["scopes"])
            self.browse_scope.set(selected)
            self._listed_scope = selected
            documents = result["documents"]
            for document in documents:
                document_id = document["document_id"]
                self.documents.insert("", "end", iid=document_id, values=(document["source"], document_id))
            summary = f"范围 {selected}　{len(documents)} 份资料版本" if selected else "库里暂无资料范围"
            self.status.set(prefix + summary)
            self._update_controls()

        self._start_task(
            {"operation": "list", "scope": scope}, prefix + "正在读取资料列表", show_documents, self.preview,
            failure_message=prefix + "读取资料列表失败",
        )

    def _view_selected(self) -> None:
        document_id = self._selected_document()
        if document_id is None:
            return
        scope = self._listed_scope
        self._clear_preview()

        def show_chunks(result: dict) -> None:
            if self._listed_scope != scope or self._selected_document() != document_id:
                self.status.set("已切换所选资料")
                return
            chunks = result["chunks"]
            if not chunks:
                self.status.set("该资料已不存在，请刷新列表")
                return
            self.preview_title.set(f"正文片段　共 {len(chunks)} 个")
            self._show(self.preview, "\n\n".join(
                f"片段 {chunk['chunk_index'] + 1}\n{chunk['text']}" for chunk in chunks
            ))
            self.close_preview.state(["!disabled"])
            self.status.set("已读取所选版本")

        self._start_task(
            {"operation": "view", "scope": scope, "document_id": document_id},
            "正在读取正文", show_chunks, self.preview,
        )

    def _delete_selected(self) -> None:
        document_id = self._selected_document()
        if document_id is None or self._pending is not None or self._closing:
            return
        scope = self._listed_scope
        source = self.documents.set(document_id, "source")
        if not messagebox.askyesno(
            "删除资料版本",
            f"资料来源 {source}\n资料范围 {scope}\n文档 ID {document_id}\n\n删除此版本的全部片段？",
            parent=self, default=messagebox.NO,
        ):
            return

        def remove_document(_result: dict) -> None:
            self._refresh_documents(notice="所选资料版本已删除")

        self._start_task(
            {"operation": "delete", "scope": scope, "document_id": document_id},
            "正在删除资料", remove_document, self.preview,
        )

    def _choose_files(self) -> None:
        patterns = " ".join(f"*{suffix}" for suffix in SUPPORTED_SUFFIXES)
        selected = filedialog.askopenfilenames(
            parent=self,
            title="选择资料",
            filetypes=(("支持的资料", patterns), ("所有文件", "*.*")),
        )
        if selected:
            self._files = selected
            self._show(self.files_box, "\n".join(selected))

    def _submit(self) -> None:
        if self._pending is not None or self._closing:
            return
        request = {"scope": self.scope.get().strip()}
        if self.tabs.select() == str(self.paste_tab):
            source, text = self.source.get().strip(), self.text.get("1.0", "end-1c")
            if not source or not text.strip():
                self.status.set("请填写资料来源和正文")
                return
            request.update(source=source, text=text)
        elif self._files:
            request["files"] = self._files
        else:
            self.status.set("请选择资料文件")
            return

        self._show(self.results_box, "")
        self._start_task(request, "正在写入资料", self._finish_write, self.results_box)

    def _start_task(
        self, request: dict, message: str, on_success: Callable[[dict], None], error_box: ScrolledText,
        *, failure_message: str = "操作失败",
    ) -> None:
        if self._pending is not None or self._closing:
            return
        self.progress.start(12)
        self.status.set(message)
        self._pending = self._executor.submit(_run_worker, request)
        self._update_controls()
        self.after(100, self._check_task, on_success, error_box, failure_message)

    def _check_task(
        self, on_success: Callable[[dict], None], error_box: ScrolledText, failure_message: str,
    ) -> None:
        if not self._pending.done():
            self.after(100, self._check_task, on_success, error_box, failure_message)
            return
        try:
            result = self._pending.result()
        except Exception:
            result = {"error": "本地资料操作失败"}
        self._pending = None
        self.progress.stop()
        if self._closing:
            self._close()
            return
        self._update_controls()
        if "error" in result:
            self.status.set(failure_message)
            self._show(error_box, result["error"])
        else:
            on_success(result)

    def _finish_write(self, result: dict) -> None:
        items = result["results"]
        lines = []
        successful = 0
        for item in items:
            if "error" in item:
                lines.append(f"{item['source']}　写入失败　{item['error']}")
            else:
                successful += 1
                lines.append(f"{item['source']}　已写入 {item['chunks']} 个片段")
        self.status.set(f"写入完成　{successful}/{len(items)} 项成功")
        self._show(self.results_box, "\n".join(lines))
        if successful:
            self._clear_documents()
            self._section_changed()

    def _close(self) -> None:
        if self._pending is not None:
            self._closing = True
            self.status.set("当前操作完成后关闭")
            return
        self._executor.shutdown(wait=False)
        self.destroy()


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        _worker_main()
    else:
        RagWriter().mainloop()

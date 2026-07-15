"""Tk GUI for the streaming NCM extractor.

SPDX-License-Identifier: GPL-2.0-or-later
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from dataclasses import dataclass
from typing import Iterable

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Drag-and-drop is optional when running from source.
    DND_FILES = None
    TkinterDnD = None

try:
    from ncm_core import CancelledError, NCMError, extract_ncm, probe_ncm
except ImportError:  # pragma: no cover - package-style source execution
    from .ncm_core import CancelledError, NCMError, extract_ncm, probe_ncm


APP_NAME = "NCM 音乐转换器"
APP_VERSION = "1.0.0"
FONT_FAMILY = "Microsoft YaHei UI"


def _enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("ncm_converter")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        log_dir = _app_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "ncm-converter.log",
            maxBytes=1_500_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


LOGGER = _configure_logging()


@dataclass
class QueueItem:
    source: Path
    source_root: Path | None = None
    iid: str = ""
    output_path: Path | None = None


def _normal_key(path: Path) -> str:
    value = str(path.resolve(strict=False))
    return os.path.normcase(value)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class ConverterApp:
    def __init__(self, root: tk.Tk, initial_paths: Iterable[str] = ()) -> None:
        self.root = root
        self.root.title(f"{APP_NAME}  {APP_VERSION}")
        self.root.geometry("1000x735")
        self.root.minsize(860, 620)
        self.root.option_add("*Font", (FONT_FAMILY, 10))

        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.scan_cancel_event = threading.Event()
        self.scan_worker: threading.Thread | None = None
        self.scanning = False
        self.scan_added = 0
        self.closing = False
        self.items_by_iid: dict[str, QueueItem] = {}
        self.iid_by_key: dict[str, str] = {}
        self.last_output_dir: Path | None = None
        self._iid_counter = 0

        settings = self._load_settings()
        self.output_mode = tk.StringVar(value=settings.get("output_mode", "same"))
        self.output_dir = tk.StringVar(value=settings.get("output_dir", ""))
        self.collision_label = tk.StringVar(
            value=settings.get("collision", "自动重命名（推荐）")
        )
        self.fetch_cover = tk.BooleanVar(value=bool(settings.get("fetch_cover", False)))
        self.summary_text = tk.StringVar(value="尚未添加文件")
        self.progress_text = tk.StringVar(value="准备就绪")

        self._build_style()
        self._build_ui()
        self._setup_drop_target()
        self._toggle_output_controls()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain_events)

        initial = [p for p in initial_paths if p and not p.startswith("-")]
        if initial:
            self.root.after(150, lambda: self._add_paths(initial))

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")
        style.configure("Title.TLabel", font=(FONT_FAMILY, 21, "bold"))
        style.configure("Subtitle.TLabel", foreground="#475569")
        style.configure("Section.TLabel", font=(FONT_FAMILY, 11, "bold"))
        style.configure("Hint.TLabel", foreground="#64748b")
        style.configure("Success.TLabel", foreground="#166534")
        style.configure("Treeview", rowheight=29)
        style.configure("Treeview.Heading", font=(FONT_FAMILY, 10, "bold"))
        style.configure("Accent.TButton", font=(FONT_FAMILY, 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(22, 18, 22, 14))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="从 NCM 原样提取 FLAC / MP3，不转码、不降低音质",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(
            header, text="本地处理 · 默认保留源文件", style="Success.TLabel"
        ).grid(row=0, column=1, rowspan=2, sticky="e")

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.add_files_button = ttk.Button(
            toolbar, text="添加 NCM 文件", command=self._choose_files
        )
        self.add_files_button.pack(side="left")
        self.add_folder_button = ttk.Button(
            toolbar, text="添加文件夹", command=self._choose_folder
        )
        self.add_folder_button.pack(side="left", padx=(8, 0))
        self.remove_button = ttk.Button(
            toolbar, text="移除选中", command=self._remove_selected
        )
        self.remove_button.pack(side="left", padx=(18, 0))
        self.clear_button = ttk.Button(toolbar, text="清空", command=self._clear_items)
        self.clear_button.pack(side="left", padx=(8, 0))
        ttk.Label(toolbar, textvariable=self.summary_text, style="Hint.TLabel").pack(
            side="right"
        )

        list_frame = ttk.Frame(outer)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ("name", "folder", "size", "format", "status")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = {
            "name": "文件名",
            "folder": "所在文件夹",
            "size": "大小",
            "format": "实际格式",
            "status": "状态",
        }
        widths = {"name": 250, "folder": 330, "size": 85, "format": 85, "status": 130}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(
                col,
                width=widths[col],
                minwidth=65,
                anchor="w" if col in ("name", "folder", "status") else "center",
                stretch=col in ("name", "folder"),
            )
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.tag_configure("even", background="#f8fafc")

        options = ttk.LabelFrame(outer, text=" 输出设置 ", padding=(14, 10))
        options.grid(row=3, column=0, sticky="ew", pady=(12, 8))
        options.columnconfigure(2, weight=1)
        self.same_radio = ttk.Radiobutton(
            options,
            text="保存到源文件旁",
            variable=self.output_mode,
            value="same",
            command=self._toggle_output_controls,
        )
        self.same_radio.grid(row=0, column=0, sticky="w")
        self.custom_radio = ttk.Radiobutton(
            options,
            text="指定目录",
            variable=self.output_mode,
            value="custom",
            command=self._toggle_output_controls,
        )
        self.custom_radio.grid(row=0, column=1, sticky="w", padx=(18, 8))
        self.output_entry = ttk.Entry(options, textvariable=self.output_dir)
        self.output_entry.grid(row=0, column=2, sticky="ew")
        self.output_button = ttk.Button(options, text="浏览…", command=self._choose_output)
        self.output_button.grid(row=0, column=3, padx=(8, 0))

        ttk.Label(options, text="同名文件：").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.collision_box = ttk.Combobox(
            options,
            textvariable=self.collision_label,
            state="readonly",
            width=20,
            values=("自动重命名（推荐）", "跳过已有文件", "覆盖已有文件"),
        )
        self.collision_box.grid(row=1, column=1, sticky="w", pady=(10, 0))
        self.cover_check = ttk.Checkbutton(
            options,
            text="缺少封面时联网补全（仅访问网易图片域名）",
            variable=self.fetch_cover,
        )
        self.cover_check.grid(row=1, column=2, columnspan=2, sticky="w", pady=(10, 0))

        log_frame = ttk.Frame(outer)
        log_frame.grid(row=4, column=0, sticky="ew", pady=(2, 6))
        log_frame.columnconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=4,
            wrap="word",
            state="disabled",
            font=(FONT_FAMILY, 9),
            relief="solid",
            borderwidth=1,
        )
        self.log_text.grid(row=0, column=0, sticky="ew")
        self._append_log("可添加单个文件或文件夹；文件夹会递归查找 .ncm。")
        if DND_FILES:
            self._append_log("也可以把 NCM 文件或文件夹直接拖入窗口。")

        footer = ttk.Frame(outer)
        footer.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        footer.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        ttk.Label(footer, textvariable=self.progress_text, width=23).grid(
            row=0, column=1, sticky="e"
        )
        self.open_button = ttk.Button(
            footer, text="打开输出目录", command=self._open_output_dir
        )
        self.open_button.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.cancel_button = ttk.Button(
            footer, text="取消", command=self._cancel, state="disabled"
        )
        self.cancel_button.grid(row=1, column=1, sticky="e", pady=(10, 0))
        self.start_button = ttk.Button(
            footer, text="开始转换", command=self._start, style="Accent.TButton"
        )
        self.start_button.grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(10, 0))

    def _setup_drop_target(self) -> None:
        if not DND_FILES:
            return
        try:
            for widget in (self.root, self.tree):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as exc:
            LOGGER.warning("Drag-and-drop unavailable: %s", exc)

    def _on_drop(self, event) -> None:
        try:
            paths = self.root.tk.splitlist(event.data)
        except tk.TclError:
            paths = [event.data]
        self._add_paths(paths)

    def _choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 NCM 文件", filetypes=(("网易云 NCM", "*.ncm"), ("所有文件", "*.*"))
        )
        self._add_paths(paths)

    def _choose_folder(self) -> None:
        path = filedialog.askdirectory(title="选择包含 NCM 的文件夹")
        if path:
            self._add_paths([path])

    def _choose_output(self) -> None:
        initial = self.output_dir.get() or str(Path.home())
        path = filedialog.askdirectory(title="选择输出目录", initialdir=initial)
        if path:
            self.output_dir.set(path)
            self.output_mode.set("custom")
            self._toggle_output_controls()

    def _add_paths(self, paths: Iterable[str | os.PathLike[str]]) -> None:
        if self.running or self.scanning:
            return
        pending = [str(path) for path in paths]
        if not pending:
            return
        self.scan_added = 0
        self.scan_cancel_event.clear()
        self._set_scanning(True)
        self.root.configure(cursor="watch")
        self.progress_text.set("正在扫描文件…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.scan_worker = threading.Thread(
            target=self._scan_paths_worker,
            args=(pending, set(self.iid_by_key)),
            daemon=False,
            name="ncm-folder-scanner",
        )
        self.scan_worker.start()

    def _scan_paths_worker(self, paths: list[str], seen: set[str]) -> None:
        invalid = 0

        def enqueue(source: Path, source_root: Path | None) -> None:
            if self.scan_cancel_event.is_set():
                return
            try:
                resolved = source.resolve(strict=False)
                key = _normal_key(resolved)
                size = resolved.stat().st_size
            except (OSError, RuntimeError):
                return
            if key in seen:
                return
            seen.add(key)
            self.events.put(("scan_file", resolved, source_root, size))

        try:
            for raw in paths:
                if self.scan_cancel_event.is_set():
                    break
                try:
                    path = Path(raw).expanduser()
                    if path.is_dir():
                        source_root = path.resolve(strict=False)
                        for current, dirs, files in os.walk(
                            source_root,
                            followlinks=False,
                        ):
                            if self.scan_cancel_event.is_set():
                                break
                            current_path = Path(current)
                            dirs[:] = [
                                name
                                for name in dirs
                                if not (current_path / name).is_symlink()
                            ]
                            for name in files:
                                if self.scan_cancel_event.is_set():
                                    break
                                if name.lower().endswith(".ncm"):
                                    enqueue(current_path / name, source_root)
                    elif path.is_file() and path.suffix.lower() == ".ncm":
                        enqueue(path, None)
                    else:
                        invalid += 1
                except (OSError, RuntimeError) as exc:
                    invalid += 1
                    self.events.put(("log", f"扫描路径失败：{raw}：{exc}"))
        except Exception as exc:
            LOGGER.exception("Unexpected folder scan failure")
            self.events.put(("log", f"文件扫描出现未预期错误：{exc}"))
        finally:
            self.events.put(
                ("scan_done", invalid, self.scan_cancel_event.is_set())
            )

    def _add_scanned_file(
        self,
        source: Path,
        source_root: Path | None,
        size: int,
    ) -> None:
        key = _normal_key(source)
        if key in self.iid_by_key:
            return
        self._iid_counter += 1
        iid = f"ncm-{self._iid_counter}"
        item = QueueItem(source=source, source_root=source_root, iid=iid)
        tag = "even" if len(self.items_by_iid) % 2 else ""
        self.tree.insert(
            "",
            "end",
            iid=iid,
            values=(source.name, str(source.parent), _human_size(size), "待检测", "等待中"),
            tags=(tag,),
        )
        self.items_by_iid[iid] = item
        self.iid_by_key[key] = iid
        self.scan_added += 1

    def _finish_scanning(self, invalid: int, cancelled: bool) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.root.configure(cursor="")
        self._set_scanning(False)
        total = len(self.items_by_iid)
        self.summary_text.set(f"共 {total} 个文件")
        if self.scan_added:
            self._append_log(f"已添加 {self.scan_added} 个 NCM 文件。")
        elif not cancelled:
            self._append_log("没有发现新的 NCM 文件。")
        if invalid:
            self._append_log(f"已忽略 {invalid} 个无效路径或非 NCM 文件。")
        if cancelled:
            self._append_log("文件扫描已取消。")
            self.progress_text.set("扫描已取消")
        else:
            self.progress_text.set("准备就绪")

    def _add_file(self, source: Path, source_root: Path | None) -> int:
        source = source.resolve(strict=False)
        key = _normal_key(source)
        if key in self.iid_by_key:
            return 0
        try:
            size = source.stat().st_size
        except OSError:
            return 0
        self._iid_counter += 1
        iid = f"ncm-{self._iid_counter}"
        item = QueueItem(source=source, source_root=source_root, iid=iid)
        tag = "even" if len(self.items_by_iid) % 2 else ""
        self.tree.insert(
            "",
            "end",
            iid=iid,
            values=(source.name, str(source.parent), _human_size(size), "待检测", "等待中"),
            tags=(tag,),
        )
        self.items_by_iid[iid] = item
        self.iid_by_key[key] = iid
        return 1

    def _remove_selected(self) -> None:
        if self.running:
            return
        for iid in self.tree.selection():
            item = self.items_by_iid.pop(iid, None)
            if item:
                self.iid_by_key.pop(_normal_key(item.source), None)
            self.tree.delete(iid)
        self.summary_text.set(f"共 {len(self.items_by_iid)} 个文件")

    def _clear_items(self) -> None:
        if self.running:
            return
        self.tree.delete(*self.tree.get_children())
        self.items_by_iid.clear()
        self.iid_by_key.clear()
        self.summary_text.set("尚未添加文件")
        self.progress.configure(value=0)
        self.progress_text.set("准备就绪")

    def _toggle_output_controls(self) -> None:
        enabled = (
            self.output_mode.get() == "custom"
            and not self.running
            and not self.scanning
        )
        state = "normal" if enabled else "disabled"
        self.output_entry.configure(state=state)
        self.output_button.configure(state=state)

    def _set_running(self, running: bool) -> None:
        self.running = running
        normal = "disabled" if running else "normal"
        for widget in (
            self.add_files_button,
            self.add_folder_button,
            self.remove_button,
            self.clear_button,
            self.same_radio,
            self.custom_radio,
            self.cover_check,
        ):
            widget.configure(state=normal)
        self.collision_box.configure(state="disabled" if running else "readonly")
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        self._toggle_output_controls()

    def _set_scanning(self, scanning: bool) -> None:
        self.scanning = scanning
        normal = "disabled" if scanning else "normal"
        for widget in (
            self.add_files_button,
            self.add_folder_button,
            self.remove_button,
            self.clear_button,
            self.same_radio,
            self.custom_radio,
            self.cover_check,
        ):
            widget.configure(state=normal)
        self.collision_box.configure(state="disabled" if scanning else "readonly")
        self.start_button.configure(state="disabled" if scanning else "normal")
        self.cancel_button.configure(state="normal" if scanning else "disabled")
        self._toggle_output_controls()

    def _start(self) -> None:
        if self.running or not self.items_by_iid:
            if not self.items_by_iid:
                messagebox.showinfo(APP_NAME, "请先添加至少一个 NCM 文件。")
            return
        custom_dir: Path | None = None
        if self.output_mode.get() == "custom":
            value = self.output_dir.get().strip()
            if not value:
                messagebox.showwarning(APP_NAME, "请选择输出目录。")
                return
            custom_dir = Path(value).expanduser().resolve(strict=False)
            try:
                custom_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"无法创建输出目录：\n{exc}")
                return
            if not custom_dir.is_dir():
                messagebox.showerror(APP_NAME, "指定的输出路径不是文件夹。")
                return

        collision = {
            "自动重命名（推荐）": "rename",
            "跳过已有文件": "skip",
            "覆盖已有文件": "overwrite",
        }.get(self.collision_label.get(), "rename")
        items = list(self.items_by_iid.values())
        for item in items:
            self._update_item(item.iid, status="等待中", fmt="待检测")
            item.output_path = None
        self.progress.configure(value=0)
        self.progress_text.set(f"0 / {len(items)}")
        self.cancel_event.clear()
        self._save_settings()
        self._set_running(True)
        self._append_log("开始转换：音频将原样提取，不进行转码。")
        LOGGER.info("Batch start | files=%d | collision=%s", len(items), collision)
        self.worker = threading.Thread(
            target=self._worker_main,
            args=(items, custom_dir, collision, bool(self.fetch_cover.get())),
            daemon=False,
            name="ncm-converter-worker",
        )
        self.worker.start()

    def _worker_main(
        self,
        items: list[QueueItem],
        custom_root: Path | None,
        collision: str,
        fetch_cover: bool,
    ) -> None:
        counts = {"success": 0, "warning": 0, "failed": 0, "skipped": 0}
        cancelled = False
        total_files = len(items)
        for index, item in enumerate(items):
            if self.cancel_event.is_set():
                cancelled = True
                break
            self.events.put(("item", item.iid, "检测中", None))
            try:
                probe = probe_ncm(item.source)
                fmt = str(probe.format).upper()
                self.events.put(("item", item.iid, "提取中", fmt))
                target_dir = self._target_dir(item, custom_root)

                def progress(done: int, total: int) -> None:
                    fraction = (done / total) if total else 0.0
                    overall = ((index + fraction) / total_files) * 100.0
                    self.events.put(("progress", overall, index, total_files, item.source.name))

                result = extract_ncm(
                    item.source,
                    output_dir=target_dir,
                    collision=collision,
                    fetch_cover=fetch_cover,
                    cancel_event=self.cancel_event,
                    progress_cb=progress,
                )
                item.output_path = result.output_path
                if result.output_path:
                    self.last_output_dir = Path(result.output_path).parent
                warnings = list(result.warnings or ())
                if result.skipped:
                    counts["skipped"] += 1
                    status = "已跳过"
                elif warnings:
                    counts["warning"] += 1
                    status = "完成（有提示）"
                    self.events.put(("log", f"{item.source.name}：" + "；".join(warnings)))
                else:
                    counts["success"] += 1
                    status = "完成"
                self.events.put(("item", item.iid, status, str(result.format).upper()))
                LOGGER.info(
                    "Converted | source=%s | output=%s | warnings=%s",
                    item.source,
                    result.output_path,
                    len(warnings),
                )
            except CancelledError:
                cancelled = True
                self.events.put(("item", item.iid, "已取消", None))
                break
            except (NCMError, OSError, ValueError) as exc:
                counts["failed"] += 1
                self.events.put(("item", item.iid, "失败", None))
                self.events.put(("log", f"{item.source.name}：{exc}"))
                LOGGER.exception("Conversion failed | source=%s", item.source)
            except Exception as exc:  # Keep one bad file from stopping a batch.
                counts["failed"] += 1
                self.events.put(("item", item.iid, "失败", None))
                self.events.put(("log", f"{item.source.name}：未预期错误：{exc}"))
                LOGGER.exception("Unexpected conversion failure | source=%s", item.source)
            self.events.put(("progress", ((index + 1) / total_files) * 100, index + 1, total_files, ""))
        self.events.put(("done", counts, cancelled))

    @staticmethod
    def _target_dir(item: QueueItem, custom_root: Path | None) -> Path | None:
        if custom_root is None:
            return None
        if item.source_root:
            try:
                relative_parent = item.source.parent.relative_to(item.source_root)
                return custom_root / relative_parent
            except ValueError:
                pass
        return custom_root

    def _cancel(self) -> None:
        if self.scanning:
            self.scan_cancel_event.set()
            self.progress_text.set("正在取消扫描…")
            self.cancel_button.configure(state="disabled")
        elif self.running:
            self.cancel_event.set()
            self.progress_text.set("正在取消…")
            self.cancel_button.configure(state="disabled")
            self._append_log("已请求取消；当前临时文件会被清理。")

    def _drain_events(self) -> None:
        handled = 0
        try:
            while handled < 250:
                event = self.events.get_nowait()
                handled += 1
                kind = event[0]
                if kind == "scan_file":
                    _, source, source_root, size = event
                    self._add_scanned_file(source, source_root, size)
                elif kind == "scan_done":
                    self._finish_scanning(event[1], event[2])
                elif kind == "item":
                    _, iid, status, fmt = event
                    self._update_item(iid, status=status, fmt=fmt)
                elif kind == "progress":
                    _, value, completed, total, name = event
                    self.progress.configure(value=max(0, min(100, value)))
                    suffix = f" · {name}" if name else ""
                    self.progress_text.set(f"{completed} / {total}{suffix}")
                elif kind == "log":
                    self._append_log(event[1])
                elif kind == "done":
                    self._finish(event[1], event[2])
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _update_item(self, iid: str, status: str | None = None, fmt: str | None = None) -> None:
        if not self.tree.exists(iid):
            return
        values = list(self.tree.item(iid, "values"))
        if fmt is not None:
            values[3] = fmt
        if status is not None:
            values[4] = status
        self.tree.item(iid, values=values)
        self.tree.see(iid)

    def _finish(self, counts: dict[str, int], cancelled: bool) -> None:
        self._set_running(False)
        done = counts["success"] + counts["warning"]
        self.progress_text.set("已取消" if cancelled else "转换完成")
        if not cancelled:
            self.progress.configure(value=100)
        summary = (
            f"成功 {done}（其中提示 {counts['warning']}），"
            f"跳过 {counts['skipped']}，失败 {counts['failed']}"
        )
        prefix = "任务已取消。" if cancelled else "批量任务完成。"
        self._append_log(f"{prefix}{summary}。")
        LOGGER.info("Batch done | cancelled=%s | %s", cancelled, summary)
        if counts["failed"] and not cancelled:
            messagebox.showwarning(APP_NAME, f"转换完成，但有文件失败。\n\n{summary}\n\n详细原因见窗口日志。")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _open_output_dir(self) -> None:
        path: Path | None = None
        if self.output_mode.get() == "custom" and self.output_dir.get().strip():
            path = Path(self.output_dir.get().strip())
        elif self.last_output_dir:
            path = self.last_output_dir
        elif self.items_by_iid:
            path = next(iter(self.items_by_iid.values())).source.parent
        if not path or not path.exists():
            messagebox.showinfo(APP_NAME, "当前没有可打开的输出目录。")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开目录：\n{exc}")

    def _settings_path(self) -> Path:
        return _app_dir() / "settings.json"

    def _load_settings(self) -> dict:
        try:
            data = json.loads(self._settings_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        data = {
            "output_mode": self.output_mode.get(),
            "output_dir": self.output_dir.get(),
            "collision": self.collision_label.get(),
            "fetch_cover": bool(self.fetch_cover.get()),
        }
        try:
            self._settings_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def _on_close(self) -> None:
        if self.closing:
            return
        if self.running or self.scanning:
            if not messagebox.askyesno(APP_NAME, "任务仍在进行。确定取消并退出吗？"):
                return
            self.closing = True
            self.cancel_event.set()
            self.scan_cancel_event.set()
            self.progress_text.set("正在取消并清理临时文件…")
            self.cancel_button.configure(state="disabled")
            self.root.protocol("WM_DELETE_WINDOW", lambda: None)
            self._wait_for_worker_then_close()
            return
        self._finalize_close()

    def _wait_for_worker_then_close(self) -> None:
        conversion_alive = self.worker is not None and self.worker.is_alive()
        scan_alive = self.scan_worker is not None and self.scan_worker.is_alive()
        if conversion_alive or scan_alive:
            self.root.after(50, self._wait_for_worker_then_close)
            return
        self._finalize_close()

    def _finalize_close(self) -> None:
        self._save_settings()
        self.root.destroy()


def _run_packaged_conversion_self_test(argv: list[str]) -> int:
    """Exercise the frozen core and tag dependencies without opening the GUI."""

    if len(argv) != 3:
        return 64
    try:
        result = extract_ncm(
            argv[1],
            output_dir=argv[2],
            collision="overwrite",
            fetch_cover=False,
            write_tags=True,
        )
        if result.skipped or result.output_path is None or not result.output_path.is_file():
            return 1
        return 0
    except Exception:
        LOGGER.exception("Packaged conversion self-test failed")
        return 1


def main(argv: list[str] | None = None) -> int:
    _enable_windows_dpi_awareness()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--self-test-convert":
        return _run_packaged_conversion_self_test(argv)
    smoke_test = "--smoke-test" in argv
    argv = [arg for arg in argv if arg != "--smoke-test"]
    if TkinterDnD is not None:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    ConverterApp(root, initial_paths=argv)
    if smoke_test:
        root.after(300, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

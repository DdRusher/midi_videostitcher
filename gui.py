"""Chinese desktop interface for MIDI-driven video stitching.

All MIDI and rendering work runs outside Tk's main thread.  The renderer owns
temporary files and publishes the finished video only after a successful export.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
from typing import Callable

from notes import MidiData, load_midi
from renderer import RenderOptions, find_ffmpeg, render_midi, validate_assets


RESOLUTIONS = {
    "720p · 1280 × 720": (1280, 720),
    "1080p · 1920 × 1080": (1920, 1080),
    "480p · 854 × 480": (854, 480),
}
CHORD_POLICIES = {
    "最高音": "highest",
    "最低音": "lowest",
    "最后进入的音符": "latest",
}


def _duration_text(seconds: float) -> str:
    minutes, remainder = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{remainder:05.2f}"


class StitcherApp(tk.Tk):
    """One self-contained application window; creating it does not start work."""

    def __init__(self) -> None:
        super().__init__()
        self.title("MIDI 视频拼接器")
        width = min(1020, max(860, self.winfo_screenwidth() - 80))
        height = min(860, max(720, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(860, 720)
        self.configure(background="#f3f5fa")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._events: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._job_id = 0
        self._busy = False
        self._job_kind: str | None = None
        self._closing = False
        self._controls: list[tuple[ttk.Widget, str]] = []
        self._full_data: MidiData | None = None
        self._loaded_path: Path | None = None
        self._loaded_signature: tuple[int, int] | None = None
        self._track_indices: list[int] = []
        self._suggested_output = ""
        self._last_output: Path | None = None
        self._last_log_message = ""
        self._poll_after_id: str | None = None

        self.midi_path = tk.StringVar()
        self.video_dir = tk.StringVar()
        self.output_path = tk.StringVar()
        self.audio_path = tk.StringVar()
        self.track_choice = tk.StringVar()
        self.channel_choice = tk.StringVar(value="全部通道")
        self.resolution = tk.StringVar(value=next(iter(RESOLUTIONS)))
        self.fps = tk.StringVar(value="30")
        self.chord_policy = tk.StringVar(value="最高音")
        self.clip_audio = tk.BooleanVar(value=True)
        self.summary = tk.StringVar(value="选择 MIDI 文件后，点击“读取 MIDI”。")
        self.asset_summary = tk.StringVar(value="素材文件名示例：C4.mp4、C#4.mp4、60.mp4。")
        self.status = tk.StringVar(value="准备就绪")
        self.percent = tk.StringVar(value="0%")

        self._configure_styles()
        self._build_widgets()
        self.midi_path.trace_add("write", self._on_midi_path_edit)
        self.video_dir.trace_add("write", self._on_assets_edit)
        self._poll_after_id = self.after(80, self._poll_events)

    def _configure_styles(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            font.nametofont(name).configure(family="Microsoft YaHei UI", size=9)
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f3f5fa")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#ffffff", foreground="#26344d")
        style.configure("Header.TLabel", background="#f3f5fa", foreground="#182742",
                        font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Subtitle.TLabel", background="#f3f5fa", foreground="#69768b")
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"),
                        foreground="#223555")
        style.configure("Muted.TLabel", foreground="#728097")
        style.configure("Status.TLabel", foreground="#315efb")
        style.configure("TEntry", padding=4, fieldbackground="#ffffff")
        style.configure("TCombobox", padding=4)
        style.configure("TButton", padding=(12, 5))
        style.configure("Accent.TButton", background="#315efb", foreground="#ffffff",
                        font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 8))
        style.map("Accent.TButton", background=[("disabled", "#b8c5e3"),
                                                ("active", "#244bd2")],
                  foreground=[("disabled", "#eef2fa")])
        style.configure("TCheckbutton", background="#ffffff", padding=2)
        style.configure("Horizontal.TProgressbar", background="#315efb",
                        troughcolor="#e9edf5", borderwidth=0, thickness=9)

    def _track_control(self, widget: ttk.Widget, normal_state: str = "normal") -> ttk.Widget:
        self._controls.append((widget, normal_state))
        return widget

    def _card(self, parent: ttk.Frame, row: int, title: str) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 9))
        card.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text=title, style="Section.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        return card

    def _path_row(self, card: ttk.Frame, row: int, label: str, variable: tk.StringVar,
                  command: Callable, button_text: str = "选择…") -> ttk.Entry:
        ttk.Label(card, text=label, width=10).grid(row=row, column=0, sticky="w", pady=2)
        entry = self._track_control(ttk.Entry(card, textvariable=variable))
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 9), pady=2)
        button = self._track_control(ttk.Button(card, text=button_text, command=command))
        button.grid(row=row, column=3, sticky="e", pady=2)
        return entry

    def _build_widgets(self) -> None:
        viewport = ttk.Frame(self)
        viewport.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(viewport, highlightthickness=0, borderwidth=0,
                                 background="#f3f5fa")
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        outer = ttk.Frame(self._canvas, padding=(22, 12))
        self._content = outer
        self._canvas_window = self._canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", self._resize_content)
        self._canvas.bind("<Configure>", self._resize_content)
        self.bind("<MouseWheel>", self._scroll_content, add="+")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        heading = ttk.Frame(outer)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(heading, text="MIDI 视频拼接器", style="Header.TLabel").pack(anchor="w")
        ttk.Label(heading, text="选择音符对应的视频，让片段跟随音乐时间线。",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        files = self._card(outer, 1, "01  选择文件")
        self.midi_entry = self._path_row(files, 1, "MIDI 文件", self.midi_path,
                                        self._browse_midi)
        self._path_row(files, 2, "视频素材目录", self.video_dir, self._browse_videos)
        self._path_row(files, 3, "保存成片", self.output_path, self._browse_output)
        self._path_row(files, 4, "配乐（可选）", self.audio_path, self._browse_audio)

        selection = self._card(outer, 2, "02  选择音轨与检查素材")
        selection.columnconfigure(1, weight=3)
        selection.columnconfigure(2, weight=1)
        self.read_button = self._track_control(ttk.Button(selection, text="读取 MIDI",
                                                     command=self._read_midi))
        self.read_button.grid(row=1, column=0, sticky="w", padx=(0, 10))
        self.track_box = self._track_control(ttk.Combobox(selection, textvariable=self.track_choice,
                                                    state="readonly"), "readonly")
        self.track_box.grid(row=1, column=1, sticky="ew", padx=(0, 10))
        self.track_box.bind("<<ComboboxSelected>>", self._selection_changed)
        self.channel_box = self._track_control(ttk.Combobox(
            selection, textvariable=self.channel_choice, state="readonly", width=13,
            values=["全部通道"] + [f"通道 {index}" for index in range(1, 17)]), "readonly")
        self.channel_box.grid(row=1, column=2, sticky="ew", padx=(0, 10))
        self.channel_box.bind("<<ComboboxSelected>>", self._selection_changed)
        self.check_button = self._track_control(ttk.Button(selection, text="检查素材",
                                                      command=self._check_assets))
        self.check_button.grid(row=1, column=3)
        ttk.Label(selection, textvariable=self.summary).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 4))
        self.notes_text = tk.Text(selection, height=2, wrap="word", relief="flat",
                                  background="#f6f8fc", foreground="#50627e",
                                  padx=8, pady=5, state="disabled",
                                  font=("Microsoft YaHei UI", 9), takefocus=False)
        self.notes_text.grid(row=3, column=0, columnspan=4, sticky="ew")
        ttk.Label(selection, textvariable=self.asset_summary, style="Muted.TLabel").grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))

        export = self._card(outer, 3, "03  导出设置")
        settings = ttk.Frame(export, style="Card.TFrame")
        settings.grid(row=1, column=0, columnspan=4, sticky="ew")
        ttk.Label(settings, text="画面").grid(row=0, column=0, padx=(0, 7))
        resolution_box = self._track_control(ttk.Combobox(
            settings, textvariable=self.resolution, values=list(RESOLUTIONS),
            state="readonly", width=23), "readonly")
        resolution_box.grid(row=0, column=1, padx=(0, 18))
        ttk.Label(settings, text="帧率").grid(row=0, column=2, padx=(0, 7))
        fps_box = self._track_control(ttk.Combobox(settings, textvariable=self.fps,
                                             values=["24", "25", "30", "60"],
                                             state="readonly", width=5), "readonly")
        fps_box.grid(row=0, column=3, padx=(0, 18))
        ttk.Label(settings, text="和弦取音").grid(row=0, column=4, padx=(0, 7))
        chord_box = self._track_control(ttk.Combobox(settings, textvariable=self.chord_policy,
                                               values=list(CHORD_POLICIES),
                                               state="readonly", width=17), "readonly")
        chord_box.grid(row=0, column=5, sticky="w")
        audio_check = self._track_control(ttk.Checkbutton(
            export, text="保留素材原声（选择配乐时由配乐替代）", variable=self.clip_audio))
        audio_check.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 2))
        ttk.Label(export, text="休止保留黑屏，短素材循环；和弦显示一个音符画面，极短音符受帧率限制。",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=4,
                                              sticky="w", pady=(0, 10))
        actions = ttk.Frame(export, style="Card.TFrame")
        actions.grid(row=4, column=0, columnspan=4, sticky="ew")
        actions.columnconfigure(0, weight=1)
        progress_area = ttk.Frame(actions, style="Card.TFrame")
        progress_area.grid(row=0, column=0, sticky="ew", padx=(0, 17))
        progress_area.columnconfigure(0, weight=1)
        ttk.Label(progress_area, textvariable=self.status, style="Status.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 5))
        ttk.Label(progress_area, textvariable=self.percent, style="Muted.TLabel").grid(
            row=0, column=1, sticky="e", padx=(6, 0))
        self.progress = ttk.Progressbar(progress_area, mode="determinate", maximum=1)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.cancel_button = ttk.Button(actions, text="取消", command=self._cancel_job,
                                         state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.export_button = self._track_control(ttk.Button(
            actions, text="开始导出", style="Accent.TButton", command=self._start_export))
        self.export_button.grid(row=0, column=2)

        log_card = self._card(outer, 4, "运行记录")
        log_card.rowconfigure(1, weight=1)
        self.log_text = tk.Text(log_card, height=2, wrap="word", relief="flat",
                                background="#ffffff", foreground="#718097",
                                font=("Microsoft YaHei UI", 9), state="disabled",
                                takefocus=False)
        self.log_text.grid(row=1, column=0, columnspan=4, sticky="nsew")
        footer = ttk.Frame(outer)
        footer.grid(row=5, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="音名以 C4 = MIDI 60 为准", style="Subtitle.TLabel").grid(
            row=0, column=0, sticky="w")
        self.open_folder_button = ttk.Button(footer, text="打开输出目录",
                                             command=lambda: self._open_result(folder=True),
                                             state="disabled")
        self.open_folder_button.grid(row=0, column=1, padx=(0, 8))
        self.open_video_button = ttk.Button(footer, text="播放成片",
                                            command=self._open_result, state="disabled")
        self.open_video_button.grid(row=0, column=2)
        self._restore_controls()

    def _resize_content(self, _event=None) -> None:
        width = max(1, self._canvas.winfo_width())
        height = max(self._content.winfo_reqheight(), self._canvas.winfo_height())
        self._canvas.itemconfigure(self._canvas_window, width=width, height=height)
        self._canvas.configure(scrollregion=(0, 0, width, height))

    def _scroll_content(self, event) -> None:
        # Text areas and comboboxes keep their own native wheel behaviour.
        if isinstance(event.widget, (tk.Text, ttk.Combobox)):
            return
        if event.delta:
            direction = -1 if event.delta > 0 else 1
            self._canvas.yview_scroll(direction * max(1, abs(event.delta) // 120), "units")

    def _append_log(self, message: str) -> None:
        message = str(message).strip()
        if not message or message == self._last_log_message:
            return
        self._last_log_message = message
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 350:
            self.log_text.delete("1.0", "101.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_notes(self, text: str) -> None:
        self.notes_text.configure(state="normal")
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", text)
        self.notes_text.configure(state="disabled")

    def _browse_midi(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="选择 MIDI 文件",
                                           filetypes=[("MIDI 文件", "*.mid *.midi"),
                                                      ("所有文件", "*")])
        if path:
            if not self.output_path.get().strip() or self.output_path.get() == self._suggested_output:
                self._suggested_output = str(Path(path).with_name(Path(path).stem + "_stitched.mp4"))
                self.output_path.set(self._suggested_output)
            self.midi_path.set(path)
            self._read_midi()

    def _browse_videos(self) -> None:
        path = filedialog.askdirectory(parent=self, title="选择按音名命名的视频素材目录")
        if path:
            self.video_dir.set(path)

    def _browse_output(self) -> None:
        current = self.output_path.get().strip()
        path = filedialog.asksaveasfilename(
            parent=self, title="选择成片保存位置", defaultextension=".mp4",
            initialfile=Path(current).name if current else "output.mp4",
            initialdir=str(Path(current).parent) if current else None,
            filetypes=[("MP4 视频", "*.mp4")], confirmoverwrite=False)
        if path:
            self.output_path.set(path)

    def _browse_audio(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择配乐（取消选择后可直接清空路径）",
            filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
                       ("所有文件", "*")])
        if path:
            self.audio_path.set(path)

    def _on_midi_path_edit(self, *_args) -> None:
        if self._busy:
            return
        current = self.midi_path.get().strip()
        if self._loaded_path is not None and current:
            try:
                if Path(current).expanduser().resolve() == self._loaded_path:
                    return
            except (OSError, ValueError):
                pass
        self._full_data = None
        self._loaded_path = None
        self._loaded_signature = None
        self._track_indices = []
        self.track_box.configure(values=[])
        self.track_choice.set("")
        self.channel_choice.set("全部通道")
        self.summary.set("选择 MIDI 文件后，点击“读取 MIDI”。")
        self.asset_summary.set("请重新检查当前 MIDI 所需素材。")
        self._set_notes("")
        self._restore_controls()

    def _on_assets_edit(self, *_args) -> None:
        if not self._busy:
            self.asset_summary.set("素材文件名示例：C4.mp4、C#4.mp4、60.mp4；更换目录后请重新检查。")

    def _selection_changed(self, _event=None) -> None:
        if self._busy:
            return
        self._refresh_summary()
        self.asset_summary.set("轨道或通道已更改，请重新检查所需素材。")

    def _selected_data(self) -> MidiData:
        if self._full_data is None or self._loaded_path is None:
            raise ValueError("请先选择 MIDI 文件并点击“读取 MIDI”。")
        if Path(self.midi_path.get().strip()).expanduser().resolve() != self._loaded_path:
            raise ValueError("MIDI 文件路径已更改，请重新读取。")
        if self._loaded_signature is not None:
            try:
                stat = self._loaded_path.stat()
            except OSError as error:
                raise ValueError("MIDI 文件已移动或无法读取，请重新选择。") from error
            if (stat.st_size, stat.st_mtime_ns) != self._loaded_signature:
                raise ValueError("MIDI 文件内容已更改，请重新读取后导出。")
        selection = self.track_box.current()
        if selection < 0 or selection >= len(self._track_indices):
            raise ValueError("请选择一个包含音符的轨道。")
        track = self._track_indices[selection]
        channel = None
        if self.channel_choice.get() != "全部通道":
            channel = int(self.channel_choice.get().split()[-1]) - 1
        events = tuple(event for event in self._full_data.notes
                       if event.track == track and (channel is None or event.channel == channel))
        if not events:
            raise ValueError("这个轨道和通道没有可导出的音符，请更换选择。")
        return replace(self._full_data, notes=events)

    def _refresh_summary(self) -> None:
        try:
            data = self._selected_data()
        except ValueError as error:
            self.summary.set(str(error))
            self._set_notes("")
            return
        pitches = {event.pitch: event.name for event in data.notes}
        self.summary.set(
            f"总时长 {_duration_text(data.duration)}   ·   {len(data.notes)} 个音符事件"
            f"   ·   需要 {len(pitches)} 种音高素材")
        self._set_notes("所需音名：" + "、".join(pitches[pitch] for pitch in sorted(pitches)))

    def _restore_controls(self) -> None:
        for widget, normal_state in self._controls:
            widget.configure(state="disabled" if self._busy else normal_state)
        if not self._busy and self._full_data is None:
            for widget in (self.track_box, self.channel_box, self.check_button):
                widget.configure(state="disabled")
        self.cancel_button.configure(state="normal" if self._busy and not self._closing
                                     and not self._cancel.is_set() else "disabled")
        available = not self._busy and self._last_output is not None
        self.open_folder_button.configure(state="normal" if available else "disabled")
        self.open_video_button.configure(state="normal" if available else "disabled")

    def _start_job(self, kind: str, work: Callable) -> None:
        if self._busy or self._closing:
            return
        self._busy = True
        self._job_kind = kind
        self._job_id += 1
        job_id = self._job_id
        self._cancel = threading.Event()
        cancellation = self._cancel
        self.progress.configure(value=0)
        self.percent.set("0%")
        self._restore_controls()

        def worker() -> None:
            last_emit = [0.0, ""]

            def report(fraction: float, message: str) -> None:
                now = time.monotonic()
                if message == last_emit[1] and now - last_emit[0] < 0.1 and fraction < 1:
                    return
                last_emit[:] = [now, message]
                self._events.put((job_id, "progress", (fraction, message)))

            try:
                value = work(cancellation, report)
                self._events.put((job_id, "done", value))
            except Exception as error:
                self._events.put((job_id, "error", (str(error), cancellation.is_set())))

        self._thread = threading.Thread(target=worker, name=f"midi-stitcher-{kind}", daemon=True)
        self._thread.start()

    def _read_midi(self) -> None:
        if self._busy:
            return
        self.asset_summary.set("请重新检查当前 MIDI 所需素材。")
        value = self.midi_path.get().strip()
        if not value:
            messagebox.showinfo("选择 MIDI", "请先选择一个 .mid 或 .midi 文件。", parent=self)
            return
        try:
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise ValueError("所选 MIDI 文件不存在，或不是一个文件。")
        except (OSError, ValueError) as error:
            messagebox.showerror("无法读取 MIDI", str(error), parent=self)
            return
        self._full_data = None
        self._loaded_path = None
        self._loaded_signature = None
        self._track_indices = []
        self.track_box.configure(values=[])
        self.track_choice.set("")
        self.summary.set("正在读取 MIDI…")
        self._set_notes("")
        self.status.set("正在读取 MIDI…")
        self._append_log(f"读取 MIDI：{path.name}")

        def work(_cancel, _report):
            before = path.stat()
            data = load_midi(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("MIDI 文件在读取期间发生了变化，请重新读取。")
            return path, data, (after.st_size, after.st_mtime_ns)

        self._start_job("read", work)

    def _check_assets(self) -> None:
        if self._busy:
            return
        try:
            data = self._selected_data()
            directory = self.video_dir.get().strip()
            if not directory:
                raise ValueError("请选择视频素材目录。")
            directory_path = Path(directory).expanduser().resolve()
        except (OSError, ValueError) as error:
            messagebox.showerror("无法检查素材", str(error), parent=self)
            return
        self.status.set("正在检查素材…")

        def work(_cancel, _report):
            assets = validate_assets(data, directory_path)
            find_ffmpeg()
            return assets

        self._start_job("check", work)

    def _start_export(self) -> None:
        if self._busy:
            return
        try:
            data = self._selected_data()
            directory = self.video_dir.get().strip()
            output = self.output_path.get().strip()
            soundtrack = self.audio_path.get().strip()
            if not directory:
                raise ValueError("请选择视频素材目录。")
            if not output:
                raise ValueError("请选择成片的保存位置。")
            output_path = Path(output).expanduser().resolve()
            if output_path.suffix.lower() != ".mp4":
                raise ValueError("成片文件名必须以 .mp4 结尾。")
            if output_path.is_dir():
                raise ValueError("保存位置需要包含文件名，例如 output.mp4。")
            overwrite = False
            if output_path.exists():
                overwrite = messagebox.askyesno(
                    "覆盖已有视频", f"文件已存在：\n{output_path}\n\n是否覆盖？", parent=self)
                if not overwrite:
                    return
            width, height = RESOLUTIONS[self.resolution.get()]
            options = RenderOptions(
                video_dir=Path(directory).expanduser().resolve(), output_path=output_path,
                width=width, height=height, fps=int(self.fps.get()),
                chord_policy=CHORD_POLICIES[self.chord_policy.get()],
                audio_path=Path(soundtrack).expanduser().resolve() if soundtrack else None,
                use_clip_audio=self.clip_audio.get(), overwrite=overwrite)
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("无法开始导出", str(error), parent=self)
            return
        self.status.set("正在检查导出素材…")
        self._append_log(f"开始导出：{output_path.name}")
        self._start_job("export", lambda cancel, report: render_midi(
            data, options, on_progress=report, cancel_event=cancel))

    def _cancel_job(self) -> None:
        if self._busy and not self._cancel.is_set():
            self._cancel.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("正在停止，请稍候…")
            self._append_log("已请求取消，正在清理临时文件。")

    def _poll_events(self) -> None:
        self._poll_after_id = None
        try:
            for _ in range(200):
                job_id, kind, payload = self._events.get_nowait()
                if job_id != self._job_id:
                    continue
                if kind == "progress":
                    fraction, message = payload
                    fraction = max(0.0, min(1.0, float(fraction)))
                    self.progress.configure(value=fraction)
                    self.percent.set(f"{fraction:.0%}")
                    if not self._cancel.is_set() and not self._closing:
                        self.status.set(message)
                    self._append_log(message)
                else:
                    self._finish_job(kind, payload)
        except queue.Empty:
            pass
        if self._closing and (self._thread is None or not self._thread.is_alive()):
            self.destroy()
            return
        self._poll_after_id = self.after(80, self._poll_events)

    def destroy(self) -> None:
        # Tk does not remove pending ``after`` scripts when a root is destroyed.
        # Cancel our poll before its registered Python command disappears.
        poll_id = self.__dict__.get("_poll_after_id")
        if poll_id is not None:
            try:
                self.after_cancel(poll_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        super().destroy()

    def _finish_job(self, event_kind: str, payload) -> None:
        job_kind = self._job_kind
        self._busy = False
        self._job_kind = None
        if self._closing:
            return
        if event_kind == "done" and self._cancel.is_set() and job_kind != "export":
            self.status.set("任务已取消")
            self._append_log("任务已取消。")
            self._restore_controls()
            return
        if event_kind == "error":
            message, cancelled = payload
            if cancelled:
                self.status.set("任务已取消")
                self._append_log("任务已取消。")
            else:
                self.status.set("任务未完成，请查看提示")
                self._append_log(message)
                messagebox.showerror("操作未完成", message or "发生了未知错误。", parent=self)
        elif job_kind == "read":
            path, data, signature = payload
            self._loaded_path = path
            self._loaded_signature = signature
            self._full_data = data
            tracks = [track for track in data.tracks if track.note_count > 0]
            self._track_indices = [track.index for track in tracks]
            self.track_box.configure(values=[
                f"轨道 {track.index + 1} · {track.name or '未命名'}（{track.note_count} 音符）"
                for track in tracks])
            if tracks:
                self.track_box.current(0)
                self.channel_choice.set("全部通道")
                self._refresh_summary()
                self.status.set("MIDI 已就绪，请检查视频素材")
                self._append_log(f"读取成功：{len(tracks)} 个有音符轨道，"
                                 f"总时长 {_duration_text(data.duration)}。")
            else:
                self.track_choice.set("")
                self.summary.set("这个 MIDI 文件没有可导出的音符。")
                self._set_notes("")
                self.status.set("未找到音符，请更换 MIDI 文件")
            for warning in data.warnings:
                self._append_log(f"提示：{warning}")
        elif job_kind == "check":
            self.asset_summary.set(f"素材检查通过：已找到 {len(payload)} 种音高的视频，导出工具已就绪。")
            self.status.set("检查通过，可以开始导出")
            self._append_log(self.asset_summary.get())
        elif job_kind == "export":
            self._last_output = Path(payload)
            self.progress.configure(value=1)
            self.percent.set("100%")
            self.status.set("导出完成")
            self._append_log(f"成片已保存：{self._last_output}")
            messagebox.showinfo("导出完成", f"视频已保存：\n{self._last_output}", parent=self)
        self._restore_controls()

    def _open_result(self, folder: bool = False) -> None:
        if self._last_output is None or self._busy:
            return
        target = self._last_output.parent if folder else self._last_output
        if not target.exists():
            messagebox.showerror("无法打开", f"文件或目录已不存在：\n{target}", parent=self)
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(target))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(target)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as error:
            messagebox.showerror("无法打开", str(error), parent=self)

    def _on_close(self) -> None:
        if self._closing:
            return
        if self._busy:
            if not messagebox.askyesno("退出程序", "任务尚未完成，是否停止任务并退出？", parent=self):
                return
            self._closing = True
            self._cancel.set()
            self.status.set("正在停止任务并清理，请稍候…")
            self._restore_controls()
        else:
            self.destroy()


def launch_gui(midi_path: str | Path | None = None,
               video_dir: str | Path | None = None,
               output_path: str | Path | None = None) -> None:
    """Open the desktop application and run Tk's event loop."""
    app = StitcherApp()
    if midi_path is not None:
        app.midi_path.set(str(midi_path))
        if output_path is None:
            path = Path(midi_path)
            app._suggested_output = str(path.with_name(path.stem + "_stitched.mp4"))
            app.output_path.set(app._suggested_output)
        app.after(100, app._read_midi)
    if video_dir is not None:
        app.video_dir.set(str(video_dir))
    if output_path is not None:
        app.output_path.set(str(output_path))
    app.mainloop()


if __name__ == "__main__":
    launch_gui()

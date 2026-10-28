"""Chinese desktop UI for selecting MIDI excerpts and stitching note videos."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
from typing import Callable

from notes import MidiData, load_midi, note_name
from material_selection import MaterialSelection, material_windows
from preview_player import PreviewPlayer
from renderer import (RenderOptions, find_ffmpeg, inspect_materials, render_audio,
                      render_midi, render_preview_audio, validate_assets)
from selection import parse_time, select_midi

RESOLUTIONS = {
    "720p · 1280 × 720": (1280, 720),
    "1080p · 1920 × 1080": (1920, 1080),
    "480p · 854 × 480": (854, 480),
}
CHORD_POLICIES = {
    "同时分屏＋原声叠加": "all",
    "只保留最高音": "highest",
    "只保留最低音": "lowest",
    "只保留最后进入的音符": "latest",
}

VIDEO_MODES = {"音名 / 频率 / 力度（快速）": "labels", "视频素材分屏": "clips"}
SOUND_SOURCES = ("素材原声", "导入整段音频")
MATERIAL_MODES = {"固定起点": "fixed", "随机选段": "random"}


def _duration_text(seconds: float) -> str:
    minutes, remainder = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{remainder:05.2f}"


def _time_input(seconds: float) -> str:
    # Truncate rather than rounding a boundary beyond the actual MIDI ending.
    milliseconds = int(max(0.0, seconds)*1000+1e-8)
    minutes, remainder = divmod(milliseconds, 60000)
    return f"{minutes:02d}:{remainder//1000:02d}.{remainder%1000:03d}"


class StitcherApp(tk.Tk):
    """One application window; MIDI and media processing stay off Tk's thread."""

    def __init__(self) -> None:
        super().__init__()
        self.title("MIDI 视频拼接器")
        self.geometry(f"{min(1020, max(860, self.winfo_screenwidth()-80))}x"
                      f"{min(900, max(720, self.winfo_screenheight()-100))}")
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
        self._controls: list[tuple[tk.Widget, str]] = []
        self._full_data: MidiData | None = None
        self._loaded_path: Path | None = None
        self._loaded_signature: tuple[int, int] | None = None
        self._track_indices: list[int] = []
        self._suggested_output = ""
        self._last_output: Path | None = None
        self._last_log_message = ""
        self._poll_after_id: str | None = None
        self._suspend_changes = False
        self._drag_mode: str | None = None
        self._player = PreviewPlayer()
        self._preview_temp = tempfile.TemporaryDirectory(prefix="midi-stitcher-preview-")
        self._preview_path: Path | None = None
        self._preview_cache_key = None
        self._preview_active = False
        self._preview_job_path: Path | None = None
        self._discard_paths: set[Path] = set()
        self._cleanup_after_id: str | None = None
        self._material_selections: dict[int, MaterialSelection] = {}
        self._material_info = {}
        self._material_edit_pitch: int | None = None
        self._material_dirty = False
        self._material_loading = False
        self.midi_path = tk.StringVar()
        self.video_dir = tk.StringVar()
        self.output_path = tk.StringVar()
        self.channel_choice = tk.StringVar(value="全部通道")
        self.start_time = tk.StringVar(value="0")
        self.end_time = tk.StringVar(value="")
        self.resolution = tk.StringVar(value=next(iter(RESOLUTIONS)))
        self.fps = tk.StringVar(value="30")
        self.chord_policy = tk.StringVar(value=next(iter(CHORD_POLICIES)))
        self.video_mode = tk.StringVar(value=next(iter(VIDEO_MODES)))
        self.sound_source = tk.StringVar(value=SOUND_SOURCES[0])
        self.audio_path = tk.StringVar()
        self.respect_velocity = tk.BooleanVar(value=True)
        self.random_seed = tk.StringVar(value="0")
        self.material_mode = tk.StringVar(value=next(iter(MATERIAL_MODES)))
        self.material_start = tk.StringVar(value="0")
        self.material_end = tk.StringVar(value="")
        self.material_hint = tk.StringVar(value="检查素材后，可为每个音高选择素材区间。")
        self.velocity_hint = tk.StringVar(value="按 MIDI 力度控制原声音量：127＝原音量，64≈一半；画面同时显示力度。")
        self.summary = tk.StringVar(value="选择 MIDI 后，可选择多个轨道和时间范围。")
        self.asset_summary = tk.StringVar(value="素材命名示例：C4.mp4、C#4.mp4、60.mp4。")
        self.status = tk.StringVar(value="准备就绪")
        self.percent = tk.StringVar(value="0%")
        self._configure_styles()
        self._build_widgets()
        self.midi_path.trace_add("write", self._on_midi_path_edit)
        self.video_dir.trace_add("write", self._on_assets_edit)
        for variable in (self.start_time, self.end_time, self.channel_choice):
            variable.trace_add("write", self._on_range_edit)
        self.audio_path.trace_add("write", self._on_audio_path_edit)
        self.sound_source.trace_add("write", self._on_sound_source_edit)
        for variable in (self.material_mode, self.material_start, self.material_end):
            variable.trace_add("write", self._on_material_editor_edit)
        for variable in (self.output_path, self.resolution, self.fps, self.chord_policy,
                         self.video_mode, self.respect_velocity, self.random_seed):
            variable.trace_add("write", self._on_config_edit)
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
        style.configure("Section.TLabel", foreground="#223555",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Muted.TLabel", foreground="#728097")
        style.configure("Status.TLabel", foreground="#315efb")
        style.configure("TEntry", padding=4, fieldbackground="#ffffff")
        style.configure("TCombobox", padding=4)
        style.configure("TButton", padding=(10, 5))
        style.configure("Accent.TButton", background="#315efb", foreground="#ffffff",
                        font=("Microsoft YaHei UI", 10, "bold"), padding=(15, 8))
        style.map("Accent.TButton", background=[("disabled", "#b8c5e3"), ("active", "#244bd2")],
                  foreground=[("disabled", "#eef2fa")])
        style.configure("Horizontal.TProgressbar", background="#315efb",
                        troughcolor="#e9edf5", borderwidth=0, thickness=8)

    def _track_control(self, widget: tk.Widget, normal_state: str = "normal"):
        self._controls.append((widget, normal_state))
        return widget

    def _card(self, parent, row, title):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 9))
        card.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text=title, style="Section.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        return card

    def _path_row(self, card, row, label, variable, command):
        ttk.Label(card, text=label, width=10).grid(row=row, column=0, sticky="w", pady=2)
        entry = self._track_control(ttk.Entry(card, textvariable=variable))
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 9), pady=2)
        self._track_control(ttk.Button(card, text="选择…", command=command)).grid(
            row=row, column=3, sticky="e", pady=2)
        return entry

    def _build_widgets(self) -> None:
        # Actions remain visible when a smaller screen requires content scrolling.
        footer = ttk.Frame(self, style="Card.TFrame", padding=(22, 10))
        footer.pack(side="bottom", fill="x")
        footer.columnconfigure(0, weight=1)
        status_row = ttk.Frame(footer, style="Card.TFrame")
        status_row.grid(row=0, column=0, columnspan=6, sticky="ew", pady=(0, 5))
        status_row.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(status_row, textvariable=self.status, style="Status.TLabel")
        self.status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(status_row, textvariable=self.percent, style="Muted.TLabel").grid(
            row=0, column=1, sticky="e", padx=(8, 0))
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=1)
        self.progress.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(0, 8))
        self.preview_button = self._track_control(ttk.Button(
            footer, text="试听选区音频", command=self._start_preview))
        self.preview_button.grid(row=2, column=0, sticky="w")
        self.stop_preview_button = ttk.Button(
            footer, text="停止试听", command=self._stop_preview, state="disabled")
        self.stop_preview_button.grid(row=2, column=1, padx=(8, 16))
        self.open_video_button = ttk.Button(
            footer, text="打开结果", command=self._open_result, state="disabled")
        self.audio_export_button = self._track_control(ttk.Button(
            footer, text="仅导出音频", command=self._start_audio_export))
        self.audio_export_button.grid(row=2, column=2, padx=(0, 8))
        self.open_video_button.grid(row=2, column=3, padx=(0, 8))
        self.cancel_button = ttk.Button(
            footer, text="取消任务", command=self._cancel_job, state="disabled")
        self.cancel_button.grid(row=2, column=4, padx=(0, 8))
        self.export_button = self._track_control(ttk.Button(
            footer, text="导出视频", style="Accent.TButton", command=self._start_export))
        self.export_button.grid(row=2, column=5)

        viewport = ttk.Frame(self)
        viewport.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(viewport, highlightthickness=0, borderwidth=0, background="#f3f5fa")
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
        ttk.Label(heading, text="选择素材片段、保留 MIDI 力度，导出音频或分屏视频。",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        files = self._card(outer, 1, "01  选择文件")
        self.midi_entry = self._path_row(files, 1, "MIDI 文件", self.midi_path, self._browse_midi)
        self._path_row(files, 2, "素材目录", self.video_dir, self._browse_videos)
        self._path_row(files, 3, "保存成片", self.output_path, self._browse_output)
        self._path_row(files, 4, "导入音频", self.audio_path, self._browse_audio)
        sound_row = ttk.Frame(files, style="Card.TFrame")
        sound_row.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(sound_row, text="声音来源").pack(side="left", padx=(0, 10))
        self._track_control(ttk.Combobox(
            sound_row, textvariable=self.sound_source, values=SOUND_SOURCES, width=15,
            state="readonly"), "readonly").pack(side="left")
        ttk.Label(sound_row, text="导入音频与 MIDI 的 0 秒对齐，选区自动截取对应位置。",
                  style="Muted.TLabel").pack(side="left", padx=(12, 0))

        selection = self._card(outer, 2, "02  选择轨道与时间范围")
        selection.columnconfigure(0, weight=3)
        selection.columnconfigure(1, weight=2)
        tracks = ttk.Frame(selection, style="Card.TFrame")
        tracks.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=(0, 16))
        tracks.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(tracks, style="Card.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.read_button = self._track_control(ttk.Button(
            toolbar, text="读取 MIDI", command=self._read_midi))
        self.read_button.pack(side="left")
        self.all_tracks_button = self._track_control(ttk.Button(
            toolbar, text="全选", command=self._select_all_tracks))
        self.all_tracks_button.pack(side="left", padx=(6, 4))
        self.clear_tracks_button = self._track_control(ttk.Button(
            toolbar, text="清空", command=self._clear_tracks))
        self.clear_tracks_button.pack(side="left")
        ttk.Label(toolbar, text="点击可多选", style="Muted.TLabel").pack(side="left", padx=(8, 0))
        list_frame = ttk.Frame(tracks, style="Card.TFrame")
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        self.track_list = self._track_control(tk.Listbox(
            list_frame, height=3, width=30, selectmode=tk.MULTIPLE, exportselection=False,
            borderwidth=0, highlightthickness=1, highlightbackground="#e0e6f1",
            background="#f7f9fd", foreground="#354967", selectbackground="#315efb",
            selectforeground="#ffffff", activestyle="none", font=("Microsoft YaHei UI", 9)))
        self.track_list.grid(row=0, column=0, sticky="ew")
        track_scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.track_list.yview)
        track_scrollbar.grid(row=0, column=1, sticky="ns")
        self.track_list.configure(yscrollcommand=track_scrollbar.set)
        self.track_list.bind("<<ListboxSelect>>", self._selection_changed)

        panel = ttk.Frame(selection, style="Card.TFrame")
        panel.grid(row=1, column=2, columnspan=2, sticky="nsew")
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text="通道").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.channel_box = self._track_control(ttk.Combobox(
            panel, textvariable=self.channel_choice, state="readonly", width=14,
            values=["全部通道"] + [f"通道 {index}" for index in range(1, 17)]), "readonly")
        self.channel_box.grid(row=0, column=1, columnspan=3, sticky="ew")
        self.channel_box.bind("<<ComboboxSelected>>", self._selection_changed)
        ttk.Label(panel, text="从").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.start_entry = self._track_control(ttk.Entry(panel, textvariable=self.start_time, width=11))
        self.start_entry.grid(row=1, column=1, sticky="ew", padx=(0, 7), pady=(8, 0))
        ttk.Label(panel, text="到").grid(row=1, column=2, padx=(0, 5), pady=(8, 0))
        self.end_entry = self._track_control(ttk.Entry(panel, textvariable=self.end_time, width=11))
        self.end_entry.grid(row=1, column=3, sticky="ew", pady=(8, 0))
        ttk.Label(panel, text="秒或 mm:ss.sss；结束留空＝全曲", style="Muted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(7, 0))
        timeline_bar = ttk.Frame(selection, style="Card.TFrame")
        timeline_bar.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 2))
        timeline_bar.columnconfigure(0, weight=1)
        ttk.Label(timeline_bar, text="拖动蓝色边界调整选区；柱形表示音符密度与力度。",
                  style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.whole_song_button = self._track_control(ttk.Button(
            timeline_bar, text="选取全曲", command=self._reset_range))
        self.whole_song_button.grid(row=0, column=1, padx=(8, 0))
        self.timeline = tk.Canvas(selection, height=76, background="#f6f8fc",
                                  highlightthickness=0, cursor="sb_h_double_arrow")
        self.timeline.grid(row=3, column=0, columnspan=4, sticky="ew")
        self.timeline.bind("<Configure>", lambda _event: self._draw_timeline())
        self.timeline.bind("<ButtonPress-1>", self._timeline_press)
        self.timeline.bind("<B1-Motion>", self._timeline_drag)
        self.timeline.bind("<ButtonRelease-1>", self._timeline_release)
        self.summary_label = ttk.Label(selection, textvariable=self.summary, wraplength=800)
        self.summary_label.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 4))
        self.notes_text = tk.Text(selection, height=2, wrap="word", relief="flat",
                                  background="#f6f8fc", foreground="#50627e", padx=8, pady=4,
                                  state="disabled", font=("Microsoft YaHei UI", 9), takefocus=False)
        self.notes_text.grid(row=5, column=0, columnspan=4, sticky="ew")
        self.asset_label = ttk.Label(selection, textvariable=self.asset_summary,
                                     style="Muted.TLabel", wraplength=800)
        self.asset_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.check_button = self._track_control(ttk.Button(
            selection, text="检查所选素材", command=self._check_assets))
        self.check_button.grid(row=6, column=3, sticky="e", padx=(8, 0), pady=(6, 0))

        self._build_material_widgets(outer, 3)
        export = self._card(outer, 4, "04  画面与声音设置")
        settings = ttk.Frame(export, style="Card.TFrame")
        settings.grid(row=1, column=0, columnspan=4, sticky="ew")
        ttk.Label(settings, text="画面").grid(row=0, column=0, padx=(0, 6))
        self._track_control(ttk.Combobox(
            settings, textvariable=self.resolution, values=list(RESOLUTIONS),
            state="readonly", width=21), "readonly").grid(row=0, column=1, padx=(0, 12))
        ttk.Label(settings, text="帧率").grid(row=0, column=2, padx=(0, 6))
        self._track_control(ttk.Combobox(
            settings, textvariable=self.fps, values=["24", "25", "30", "60"],
            state="readonly", width=4), "readonly").grid(row=0, column=3, padx=(0, 12))
        ttk.Label(settings, text="重叠音符").grid(row=0, column=4, padx=(0, 6))
        self._track_control(ttk.Combobox(
            settings, textvariable=self.chord_policy, values=list(CHORD_POLICIES),
            state="readonly", width=24), "readonly").grid(row=0, column=5, sticky="w")
        mode_row = ttk.Frame(export, style="Card.TFrame")
        mode_row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(mode_row, text="画面模式").pack(side="left", padx=(0, 8))
        self._track_control(ttk.Combobox(
            mode_row, textvariable=self.video_mode, values=list(VIDEO_MODES),
            state="readonly", width=29), "readonly").pack(side="left")
        self.velocity_checkbox = self._track_control(ttk.Checkbutton(
            mode_row, text="按 MIDI 力度控制音量", variable=self.respect_velocity))
        self.velocity_checkbox.pack(side="left", padx=(18, 0))
        ttk.Label(export, textvariable=self.velocity_hint, style="Muted.TLabel", wraplength=760).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.render_hint = ttk.Label(
            export, text="视频素材模式自动分屏并叠加原声；快速模式直接显示音名、频率和力度。\n"
                         "导入整段音频时保留音乐本身的强弱。试听与导出共用同一选段和随机结果。",
            style="Muted.TLabel", wraplength=800)
        self.render_hint.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        log_card = self._card(outer, 5, "运行记录")
        log_card.rowconfigure(1, weight=1)
        self.log_text = tk.Text(log_card, height=2, wrap="word", relief="flat",
                                background="#ffffff", foreground="#718097",
                                font=("Microsoft YaHei UI", 9), state="disabled", takefocus=False)
        self.log_text.grid(row=1, column=0, columnspan=4, sticky="nsew")
        bottom = ttk.Frame(outer)
        bottom.grid(row=6, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, text="音名以 C4＝MIDI 60 为准", style="Subtitle.TLabel").grid(
            row=0, column=0, sticky="w")
        self.open_folder_button = ttk.Button(
            bottom, text="打开输出目录", command=lambda: self._open_result(folder=True), state="disabled")
        self.open_folder_button.grid(row=0, column=1)
        self._restore_controls()
        self._draw_timeline()

    def _build_material_widgets(self, outer, row) -> None:
        first_control = len(self._controls)
        card = self._card(outer, row, "03  为每种素材选择片段")
        table_frame = ttk.Frame(card, style="Card.TFrame")
        table_frame.grid(row=1, column=0, columnspan=4, sticky="ew")
        table_frame.columnconfigure(0, weight=1)
        columns = ("note", "duration", "needed", "mode", "start", "end")
        self.material_table = ttk.Treeview(table_frame, columns=columns, show="headings",
                                           height=4, selectmode="browse")
        for column, title, width in zip(columns,
                ("音名", "素材时长(s)", "MIDI 最长(s)", "选段方式", "区间起点(s)", "区间终点(s)"),
                (70, 108, 108, 108, 108, 108)):
            self.material_table.heading(column, text=title)
            self.material_table.column(column, width=width, minwidth=65, stretch=True, anchor="center")
        self.material_table.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.material_table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.material_table.configure(yscrollcommand=scroll.set)
        self.material_table.bind("<<TreeviewSelect>>", self._material_row_selected)
        editor = ttk.Frame(card, style="Card.TFrame")
        editor.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(editor, text="选中音高").grid(row=0, column=0, padx=(0, 8))
        self._track_control(ttk.Combobox(editor, textvariable=self.material_mode,
            values=list(MATERIAL_MODES), state="readonly", width=10), "readonly").grid(row=0, column=1)
        ttk.Label(editor, text="从").grid(row=0, column=2, padx=(12, 6))
        self._track_control(ttk.Entry(editor, textvariable=self.material_start, width=9)).grid(row=0, column=3)
        ttk.Label(editor, text="到").grid(row=0, column=4, padx=6)
        self._track_control(ttk.Entry(editor, textvariable=self.material_end, width=9)).grid(row=0, column=5)
        ttk.Label(editor, text="秒；结束留空＝素材末尾", style="Muted.TLabel").grid(
            row=0, column=6, padx=(8, 0))
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self._track_control(ttk.Button(actions, text="应用此音高",
            command=self._apply_material_selection)).pack(side="left")
        self._track_control(ttk.Button(actions, text="应用所有音高",
            command=lambda: self._apply_material_selection(all_pitches=True))).pack(side="left", padx=6)
        self._track_control(ttk.Button(actions, text="恢复此音高整段",
            command=self._reset_material_selection)).pack(side="left")
        ttk.Label(actions, text="随机种子").pack(side="left", padx=(18, 6))
        self._track_control(ttk.Entry(actions, textvariable=self.random_seed, width=8)).pack(side="left")
        self._track_control(ttk.Button(actions, text="换一批随机片段",
            command=self._reroll_random)).pack(side="left", padx=(6, 0))
        ttk.Label(card, textvariable=self.material_hint, style="Muted.TLabel", wraplength=760).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(7, 0))
        ttk.Label(card, text="固定：从区间起点开始；随机：在区间内挑选足够长的一段。音符更长时只循环所选区间。",
                  style="Muted.TLabel", wraplength=760).grid(row=5, column=0, columnspan=4, sticky="w", pady=(3, 0))
        self._material_controls = list(self._controls[first_control:])

    def _uses_imported_audio(self) -> bool:
        return self.sound_source.get() == SOUND_SOURCES[1]

    def _browse_audio(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="导入与 MIDI 同步的音频",
            filetypes=[("音频文件", "*.wav *.mp3 *.flac *.m4a *.aac *.ogg *.opus *.mp4"), ("所有文件", "*")])
        if path:
            self.audio_path.set(path)

    def _on_audio_path_edit(self, *_args) -> None:
        if self._busy or self._suspend_changes:
            return
        if self.audio_path.get().strip():
            self._suspend_changes = True
            try:
                self.sound_source.set(SOUND_SOURCES[1])
                self.video_mode.set(next(label for label, value in VIDEO_MODES.items() if value == "labels"))
            finally:
                self._suspend_changes = False
        self._on_sound_source_edit()

    def _on_sound_source_edit(self, *_args) -> None:
        if self._busy or self._suspend_changes:
            return
        self._invalidate_preview()
        if self._uses_imported_audio():
            self.velocity_hint.set("导入音频保留音乐本身的强弱；MIDI 力度显示在画面中，不再次改变整段音乐音量。")
        else:
            self.velocity_hint.set("按 MIDI 力度控制原声音量：127＝原音量，64≈一半；画面同时显示力度。")
        self._restore_controls()

    def _on_material_editor_edit(self, *_args) -> None:
        if not self._material_loading and not self._busy:
            self._material_dirty = True
            self._invalidate_preview()

    def _refresh_material_table(self) -> None:
        if "material_table" not in self.__dict__:
            return
        try:
            data = self._selected_data()
        except (OSError, ValueError):
            data = None
        needed = {}
        if data is not None:
            for note in data.notes:
                needed[note.pitch] = max(needed.get(note.pitch, 0), note.duration + note.source_offset)
        previous = self._material_edit_pitch
        self.material_table.delete(*self.material_table.get_children())
        for pitch in sorted(needed):
            info = self._material_info.get(pitch)
            selection = self._material_selections.get(pitch, MaterialSelection())
            duration = getattr(info, "duration", None)
            self.material_table.insert("", "end", iid=str(pitch), values=(note_name(pitch),
                f"{duration:.3f}" if duration is not None else "待检查", f"{needed[pitch]:.3f}",
                "随机选段" if selection.mode == "random" else "固定起点", f"{selection.start:g}",
                f"{selection.end:g}" if selection.end is not None else "素材末尾"))
        if needed:
            pitch = previous if previous in needed else min(needed)
            self.material_table.selection_set(str(pitch))
            if not self._material_dirty or pitch != previous:
                self._load_material_editor(pitch)
        else:
            self._material_edit_pitch = None
            self._material_dirty = False
        self.material_hint.set("点击一行编辑；检查素材后会显示实际时长。设置会用于试听和正式导出。")

    def _load_material_editor(self, pitch: int) -> None:
        selection = self._material_selections.get(pitch, MaterialSelection())
        self._material_loading = True
        try:
            self._material_edit_pitch = pitch
            self.material_mode.set(next(label for label, mode in MATERIAL_MODES.items() if mode == selection.mode))
            self.material_start.set(f"{selection.start:g}")
            self.material_end.set("" if selection.end is None else f"{selection.end:g}")
            self._material_dirty = False
        finally:
            self._material_loading = False

    def _material_row_selected(self, _event=None) -> None:
        if self._busy or self._material_loading:
            return
        selected = self.material_table.selection()
        if not selected:
            return
        pitch = int(selected[0])
        if pitch == self._material_edit_pitch:
            return
        previous = self._material_edit_pitch
        if self._material_dirty and previous is not None:
            try:
                self._save_material_editor([previous])
            except ValueError as error:
                messagebox.showerror("无法保存素材区间", str(error), parent=self)
                self.material_table.selection_set(str(previous))
                return
        self._load_material_editor(pitch)

    def _save_material_editor(self, pitches: list[int]) -> None:
        start = parse_time(self.material_start.get().strip() or "0")
        end_text = self.material_end.get().strip()
        selection = MaterialSelection(start=start, end=parse_time(end_text) if end_text else None,
                                      mode=MATERIAL_MODES[self.material_mode.get()])
        durations = {}
        for pitch in pitches:
            info = self._material_info.get(pitch)
            if info is None:
                raise ValueError("请先点击“检查所选素材”读取素材时长，再选择片段。")
            durations[pitch] = info.duration
        material_windows({pitch: selection for pitch in pitches}, durations)
        for pitch in pitches:
            self._material_selections[pitch] = selection
        self._material_dirty = False
        self._invalidate_preview()

    def _apply_material_selection(self, all_pitches: bool = False) -> None:
        if self._busy:
            return
        try:
            pitches = [int(item) for item in self.material_table.get_children()] if all_pitches else (
                [self._material_edit_pitch] if self._material_edit_pitch is not None else [])
            if not pitches:
                raise ValueError("请先读取 MIDI 并选择要使用的音符。")
            self._save_material_editor(pitches)
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("无法设置素材区间", str(error), parent=self)
            return
        self._refresh_material_table()
        self.status.set(f"已保存 {len(pitches)} 种音高的素材区间")

    def _reset_material_selection(self) -> None:
        if self._busy or self._material_edit_pitch is None:
            return
        self._material_selections.pop(self._material_edit_pitch, None)
        self._invalidate_preview()
        self._refresh_material_table()

    def _reroll_random(self) -> None:
        if self._busy:
            return
        try:
            self.random_seed.set(str(int(self.random_seed.get().strip() or "0") + 1))
        except ValueError:
            self.random_seed.set("1")
        random_active = self.material_mode.get() == "随机选段" or any(
            choice.mode == "random" for choice in self._material_selections.values())
        self.status.set("已更换随机种子，下次试听与导出将使用同一批新片段" if random_active else
                        "随机种子已更新；把选段方式设为“随机选段”后即可使用")

    def _resize_content(self, _event=None) -> None:
        width = max(1, self._canvas.winfo_width())
        height = max(self._content.winfo_reqheight(), self._canvas.winfo_height())
        self._canvas.itemconfigure(self._canvas_window, width=width, height=height)
        self._canvas.configure(scrollregion=(0, 0, width, height))
        wrap = max(300, width-80)
        for name in ("summary_label", "render_hint"):
            if name in self.__dict__:
                self.__dict__[name].configure(wraplength=wrap)
        if "asset_label" in self.__dict__:
            self.asset_label.configure(wraplength=max(250, wrap-120))
        if "status_label" in self.__dict__:
            self.status_label.configure(wraplength=max(300, self.winfo_width()-110))

    def _scroll_content(self, event) -> None:
        if isinstance(event.widget, (tk.Text, tk.Listbox, ttk.Combobox, ttk.Treeview)):
            return
        if event.delta:
            self._canvas.yview_scroll((-1 if event.delta > 0 else 1) *
                                      max(1, abs(event.delta)//120), "units")

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
            filetypes=[("MIDI 文件", "*.mid *.midi"), ("所有文件", "*")])
        if path:
            if not self.output_path.get().strip() or self.output_path.get() == self._suggested_output:
                self._suggested_output = str(Path(path).with_name(Path(path).stem+"_stitched.mp4"))
                self.output_path.set(self._suggested_output)
            self.midi_path.set(path)
            self._read_midi()

    def _browse_videos(self) -> None:
        path = filedialog.askdirectory(parent=self, title="选择按音名命名的视频或音频素材目录")
        if path:
            self.video_dir.set(path)

    def _browse_output(self) -> None:
        current = self.output_path.get().strip()
        path = filedialog.asksaveasfilename(parent=self, title="选择成片保存位置",
            defaultextension=".mp4", initialfile=Path(current).name if current else "output.mp4",
            initialdir=str(Path(current).parent) if current else None,
            filetypes=[("MP4 视频", "*.mp4")], confirmoverwrite=False)
        if path:
            self.output_path.set(path)

    def _invalidate_preview(self) -> None:
        self._stop_preview(invalidate=True)

    def _stop_preview(self, invalidate: bool = False) -> None:
        was_active = self._preview_active
        self._preview_active = False
        try:
            self._player.stop()
        except (OSError, ValueError):
            pass
        if invalidate:
            stale_path = self._preview_path
            self._preview_path = None
            self._preview_cache_key = None
            if stale_path is not None:
                self._discard_paths.add(stale_path)
                self._clean_stale_previews()
        if "stop_preview_button" in self.__dict__:
            self.stop_preview_button.configure(state="disabled")
        if was_active and not self._busy and not self._closing:
            self.status.set("试听已停止")

    def _clean_stale_previews(self) -> None:
        if self._cleanup_after_id is not None:
            try:
                self.after_cancel(self._cleanup_after_id)
            except tk.TclError:
                pass
        self._cleanup_after_id = None
        for path in tuple(self._discard_paths):
            try:
                path.unlink(missing_ok=True)
                self._discard_paths.discard(path)
            except OSError:
                pass
        if self._discard_paths and not self._closing:
            self._cleanup_after_id = self.after(200, self._clean_stale_previews)

    def _on_midi_path_edit(self, *_args) -> None:
        if self._busy or self._suspend_changes:
            return
        current = self.midi_path.get().strip()
        if self._loaded_path is not None and current:
            try:
                if Path(current).expanduser().resolve() == self._loaded_path:
                    return
            except (OSError, ValueError):
                pass
        self._invalidate_preview()
        self._clear_midi_state()
        self.status.set("请选择并读取 MIDI 文件")

    def _clear_midi_state(self) -> None:
        self._full_data = None
        self._loaded_path = None
        self._loaded_signature = None
        self._track_indices = []
        self.track_list.delete(0, "end")
        self._suspend_changes = True
        try:
            self.channel_choice.set("全部通道")
            self.start_time.set("0")
            self.end_time.set("")
        finally:
            self._suspend_changes = False
        self.summary.set("选择 MIDI 后，可选择多个轨道和时间范围。")
        self.asset_summary.set("请重新检查当前 MIDI 选区所需素材。")
        self._set_notes("")
        self._refresh_material_table()
        self._draw_timeline()
        self._restore_controls()

    def _on_assets_edit(self, *_args) -> None:
        if not self._busy and not self._suspend_changes:
            self._invalidate_preview()
            self._material_selections.clear()
            self._material_info.clear()
            self._material_edit_pitch = None
            self._material_dirty = False
            self._refresh_material_table()
            self.asset_summary.set("素材目录已更改，请重新检查当前选区所需素材。")

    def _on_config_edit(self, *_args) -> None:
        if not self._busy and not self._suspend_changes:
            self._invalidate_preview()
            self._restore_controls()

    def _on_range_edit(self, *_args) -> None:
        if not self._busy and not self._suspend_changes:
            self._selection_changed()

    def _selection_changed(self, _event=None) -> None:
        if self._busy or self._suspend_changes:
            return
        self._invalidate_preview()
        self._refresh_summary()
        self._refresh_material_table()
        self.asset_summary.set("轨道、通道或时间已更改，请重新检查选区所需素材。")
        self._restore_controls()

    def _select_all_tracks(self) -> None:
        if not self._busy:
            self.track_list.selection_set(0, "end")
            self._selection_changed()

    def _clear_tracks(self) -> None:
        if not self._busy:
            self.track_list.selection_clear(0, "end")
            self._selection_changed()

    def _reset_range(self) -> None:
        if self._busy:
            return
        self._suspend_changes = True
        try:
            self.start_time.set("0")
            self.end_time.set("")
        finally:
            self._suspend_changes = False
        self._selection_changed()

    def _selected_tracks(self) -> list[int]:
        return [self._track_indices[index] for index in self.track_list.curselection()
                if index < len(self._track_indices)]

    def _selected_channel(self) -> int | None:
        value = self.channel_choice.get()
        return None if value == "全部通道" else int(value.split()[-1])-1

    def _time_bounds(self) -> tuple[float, float | None]:
        start = parse_time(self.start_time.get().strip() or "0")
        end_text = self.end_time.get().strip()
        return start, parse_time(end_text) if end_text else None

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
                raise ValueError("MIDI 文件内容已更改，请重新读取后继续。")
        tracks = self._selected_tracks()
        if not tracks:
            raise ValueError("请至少选择一个轨道；可点击“全选”恢复。")
        channel = self._selected_channel()
        start, end = self._time_bounds()
        data = select_midi(self._full_data, tracks=tracks,
                           channels=None if channel is None else [channel], start=start, end=end)
        if not data.notes:
            raise ValueError("当前轨道、通道和时间范围没有音符，请调整选区。")
        return data

    def _refresh_summary(self) -> None:
        try:
            data = self._selected_data()
        except (OSError, ValueError) as error:
            self.summary.set(str(error))
            self._set_notes("")
            self._draw_timeline()
            return
        pitches = {event.pitch: event.name for event in data.notes}
        start, end = self._time_bounds()
        actual_end = self._full_data.duration if end is None else min(end, self._full_data.duration)
        self.summary.set(f"选区 {_duration_text(start)}–{_duration_text(actual_end)}"
            f"   ·   {len(self._selected_tracks())} 个轨道   ·   成片 {_duration_text(data.duration)}"
            f"   ·   {len(data.notes)} 个音符 / {len(pitches)} 种音高")
        velocities = [event.velocity for event in data.notes]
        self._set_notes("选区所需音名："+"、".join(pitches[pitch] for pitch in sorted(pitches))
                        +f"\nMIDI 力度：{min(velocities)}–{max(velocities)} / 127（保留每个音符自己的强弱）")
        self._draw_timeline()

    def _draw_timeline(self) -> None:
        if "timeline" not in self.__dict__:
            return
        canvas = self.timeline
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 250), max(canvas.winfo_height(), 76)
        left, right = 13.0, width-13.0
        duration = self._full_data.duration if self._full_data is not None else 0.0
        if duration <= 0:
            canvas.create_text(width/2, height/2, text="读取 MIDI 后可拖动两侧边界选择片段",
                               fill="#8491a8", font=("Microsoft YaHei UI", 9))
            return
        try:
            start, end = self._time_bounds()
            end = duration if end is None else end
        except ValueError:
            start, end = 0.0, duration
        start, end = max(0.0, min(start, duration)), max(0.0, min(end, duration))
        end = max(start, end)
        scale = (right-left)/duration
        x_start, x_end = left+start*scale, left+end*scale
        canvas.create_rectangle(x_start, 8, x_end, height-20, fill="#e2eaff", outline="")
        chosen_tracks = set(self._selected_tracks())
        channel = self._selected_channel()
        counts = [0]*min(300, max(30, int((right-left)/3)))
        for event in self._full_data.notes:
            if event.track in chosen_tracks and (channel is None or event.channel == channel):
                bucket = min(len(counts)-1, max(0, int(event.start/duration*len(counts))))
                counts[bucket] += event.velocity / 127
        maximum = max(counts, default=0)
        if maximum:
            step, baseline = (right-left)/len(counts), height-23
            for index, count in enumerate(counts):
                if count:
                    x = left+index*step
                    bar_height = max(3.0, count/maximum*(height-37))
                    color = "#5680ef" if x_start <= x <= x_end else "#bcc7dc"
                    canvas.create_rectangle(x, baseline-bar_height, x+max(1, step-1),
                                            baseline, fill=color, outline="")
        for fraction in (0, .25, .5, .75, 1):
            x = left+fraction*(right-left)
            canvas.create_text(x, height-9, text=_duration_text(duration*fraction), fill="#7a88a0",
                anchor="w" if fraction == 0 else "e" if fraction == 1 else "center",
                font=("Microsoft YaHei UI", 8))
        for x, label in ((x_start, "起"), (x_end, "止")):
            canvas.create_line(x, 7, x, height-20, fill="#315efb", width=3)
            canvas.create_rectangle(x-7, 4, x+7, 18, fill="#315efb", outline="")
            canvas.create_text(x, 11, text=label, fill="#ffffff", font=("Microsoft YaHei UI", 8))

    def _timeline_time(self, x: float) -> float:
        duration = self._full_data.duration if self._full_data else 0
        span = max(1.0, self.timeline.winfo_width()-26.0)
        return max(0.0, min(duration, (x-13.0)/span*duration))

    def _timeline_press(self, event) -> None:
        if self._busy or self._full_data is None or self._full_data.duration <= 0:
            return
        try:
            start, end = self._time_bounds()
            end = self._full_data.duration if end is None else end
        except ValueError:
            start, end = 0.0, self._full_data.duration
        clicked = self._timeline_time(event.x)
        self._drag_mode = "start" if abs(clicked-start) <= abs(clicked-end) else "end"

    def _timeline_drag(self, event) -> None:
        if self._busy or self._drag_mode is None or self._full_data is None:
            return
        duration = self._full_data.duration
        try:
            start, end = self._time_bounds()
            end = duration if end is None else min(duration, end)
        except ValueError:
            start, end = 0.0, duration
        clicked = self._timeline_time(event.x)
        minimum_span = min(.001, duration/2)
        if self._drag_mode == "start":
            start = max(0.0, min(clicked, end-minimum_span))
        else:
            end = min(duration, max(clicked, start+minimum_span))
        self._suspend_changes = True
        try:
            self.start_time.set(_time_input(start))
            self.end_time.set("" if end >= duration else _time_input(end))
        finally:
            self._suspend_changes = False
        self._selection_changed()

    def _timeline_release(self, event) -> None:
        self._timeline_drag(event)
        self._drag_mode = None

    def _restore_controls(self) -> None:
        for widget, state in self._controls:
            widget.configure(state="disabled" if self._busy or self._closing else state)
        if not self._busy and self._full_data is None:
            for widget in (self.track_list, self.channel_box, self.start_entry, self.end_entry,
                           self.all_tracks_button, self.clear_tracks_button,
                           self.whole_song_button, self.check_button, self.preview_button,
                           self.audio_export_button):
                widget.configure(state="disabled")
        no_materials = self._uses_imported_audio() and VIDEO_MODES[self.video_mode.get()] == "labels"
        if "material_table" in self.__dict__:
            self.material_table.state(["disabled"] if self._busy or self._closing or no_materials else ["!disabled"])
        if no_materials:
            for widget, _state in self.__dict__.get("_material_controls", []):
                widget.configure(state="disabled")
            self.material_hint.set("导入音频＋音名画面无需素材；素材选段设置不会改变这条音乐。")
        else:
            self.material_hint.set("点击一行编辑；检查素材后会显示实际时长。设置会用于试听和正式导出。")
        if "velocity_checkbox" in self.__dict__ and self._uses_imported_audio():
            self.velocity_checkbox.configure(state="disabled")
        self.cancel_button.configure(state="normal" if self._busy and not self._closing
            and not self._cancel.is_set() else "disabled")
        self.stop_preview_button.configure(
            state="normal" if self._preview_active and not self._closing else "disabled")
        available = not self._busy and not self._closing and self._last_output is not None
        self.open_folder_button.configure(state="normal" if available else "disabled")
        self.open_video_button.configure(state="normal" if available else "disabled")

    def _start_job(self, kind: str, work: Callable) -> None:
        if self._busy or self._closing:
            return
        self._busy, self._job_kind = True, kind
        self._job_id += 1
        job_id = self._job_id
        self._cancel = threading.Event()
        cancellation = self._cancel
        self.progress.configure(value=0)
        self.percent.set("0%")
        self._restore_controls()

        def worker():
            last_emit = [0.0, ""]

            def report(fraction, message):
                now = time.monotonic()
                if message == last_emit[1] and now-last_emit[0] < .1 and fraction < 1:
                    return
                last_emit[:] = [now, message]
                self._events.put((job_id, "progress", (fraction, message)))
            try:
                self._events.put((job_id, "done", work(cancellation, report)))
            except Exception as error:
                self._events.put((job_id, "error", (str(error), cancellation.is_set())))

        self._thread = threading.Thread(target=worker, name=f"midi-stitcher-{kind}", daemon=True)
        self._thread.start()

    def _read_midi(self) -> None:
        if self._busy:
            return
        self._invalidate_preview()
        self.asset_summary.set("请重新检查当前 MIDI 选区所需素材。")
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
        self._clear_midi_state()
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
            if self._uses_imported_audio() and VIDEO_MODES[self.video_mode.get()] == "labels":
                self._render_options(Path(self.output_path.get().strip() or "output.mp4"))
                self.asset_summary.set("导入音频＋音名画面模式无需按音高提供素材。")
                self.status.set("MIDI 与导入音频已就绪，可以试听或导出")
                return
            directory = self.video_dir.get().strip()
            if not directory:
                raise ValueError("请选择素材目录。")
            directory_path = Path(directory).expanduser().resolve()
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("无法检查素材", str(error), parent=self)
            return
        self._stop_preview()
        self.status.set("正在读取选区素材时长…")

        def work(cancel, _report):
            return inspect_materials(data, directory_path, cancel_event=cancel)
        self._start_job("check", work)

    def _render_options(self, output_path: Path, overwrite: bool = False, *, audio_only: bool = False) -> RenderOptions:
        directory = self.video_dir.get().strip()
        imported = self._uses_imported_audio()
        mode = VIDEO_MODES[self.video_mode.get()]
        if self._material_dirty and self._material_edit_pitch is not None and not (
                imported and (mode == "labels" or audio_only)):
            self._save_material_editor([self._material_edit_pitch])
            self._refresh_material_table()
        if not directory and not (imported and (mode == "labels" or audio_only)):
            raise ValueError("请选择素材目录，或使用导入音频＋音名画面模式。")
        audio = None
        start, _end = self._time_bounds()
        if imported:
            value = self.audio_path.get().strip()
            if not value:
                raise ValueError("请选择要导入的音频文件。")
            audio = Path(value).expanduser().resolve()
            if not audio.is_file():
                raise ValueError("导入的音频文件不存在，或不是一个文件。")
        active_pitches = {note.pitch for note in self._selected_data().notes}
        settings = {pitch: choice for pitch, choice in self._material_selections.items() if pitch in active_pitches}
        width, height = RESOLUTIONS[self.resolution.get()]
        return RenderOptions(video_dir=Path(directory or ".").expanduser().resolve(),
            output_path=output_path, width=width, height=height, fps=int(self.fps.get()),
            chord_policy=CHORD_POLICIES[self.chord_policy.get()],
            use_clip_audio=True, overwrite=overwrite, audio_path=audio, audio_start=start if imported else 0.0,
            video_mode=mode, respect_velocity=self.respect_velocity.get(),
            material_selections=settings if not (imported and mode == "labels") else {},
            random_seed=0 if imported and (mode == "labels" or audio_only) else
                int(self.random_seed.get().strip() or "0"))

    def _start_audio_export(self) -> None:
        if self._busy:
            return
        try:
            data = self._selected_data()
            current = Path(self.output_path.get().strip() or "output.mp4")
            chosen = filedialog.asksaveasfilename(parent=self, title="仅导出选区音频",
                defaultextension=".wav", initialfile=current.stem+"_audio.wav",
                initialdir=str(current.parent), filetypes=[("WAV 音频", "*.wav")], confirmoverwrite=False)
            if not chosen:
                return
            path = Path(chosen).expanduser().resolve()
            if path.suffix.lower() != ".wav" or path.is_dir():
                raise ValueError("请选择包含文件名的 .wav 保存位置。")
            overwrite = False
            if path.exists():
                overwrite = messagebox.askyesno("覆盖已有音频", f"文件已存在：\n{path}\n\n是否覆盖？", parent=self)
                if not overwrite:
                    return
            options = self._render_options(path, overwrite, audio_only=True)
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("无法导出音频", str(error), parent=self)
            return
        self._stop_preview()
        self.status.set("正在导出选区音频…")
        self._append_log(f"仅导出音频：{_duration_text(data.duration)}，{path.name}")
        self._start_job("audio_export", lambda cancel, report: render_audio(
            data, options, path, on_progress=report, cancel_event=cancel))

    def _start_export(self) -> None:
        if self._busy:
            return
        try:
            data = self._selected_data()
            output = self.output_path.get().strip()
            if not output:
                raise ValueError("请选择成片的保存位置。")
            path = Path(output).expanduser().resolve()
            if path.suffix.lower() != ".mp4":
                raise ValueError("成片文件名必须以 .mp4 结尾。")
            if path.is_dir():
                raise ValueError("保存位置需要包含文件名，例如 output.mp4。")
            overwrite = False
            if path.exists():
                overwrite = messagebox.askyesno(
                    "覆盖已有视频", f"文件已存在：\n{path}\n\n是否覆盖？", parent=self)
                if not overwrite:
                    return
            options = self._render_options(path, overwrite)
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("无法开始导出", str(error), parent=self)
            return
        self._stop_preview()
        self.status.set("正在检查选区导出素材…")
        self._append_log(f"导出选区：{_duration_text(data.duration)}，{path.name}")
        self._start_job("export", lambda cancel, report: render_midi(
            data, options, on_progress=report, cancel_event=cancel))

    def _start_preview(self) -> None:
        if self._busy:
            return
        try:
            data = self._selected_data()
            preview_root = Path(self._preview_temp.name)
            options = self._render_options(preview_root/"preview-source.mp4", overwrite=True, audio_only=True)
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("无法试听", str(error), parent=self)
            return
        self._stop_preview()
        cached_path, cached_key = self._preview_path, self._preview_cache_key
        destination = preview_root/f"selection-{time.time_ns()}.wav"
        self._preview_job_path = destination
        note_key = tuple((event.pitch, event.start, event.end, event.velocity, event.channel, event.track,
                          getattr(event, "source_offset", 0.0)) for event in data.notes)
        materials_key = tuple(sorted((pitch, setting.start, setting.end, setting.mode)
                                     for pitch, setting in (options.material_selections or {}).items()))
        base_key = (str(options.video_dir), options.fps, options.chord_policy, data.duration, note_key,
                    options.random_seed, options.respect_velocity, options.video_mode,
                    options.audio_start, materials_key)
        self.status.set("正在生成完整选区的音频试听…")
        self._append_log(f"生成选区音频试听：{_duration_text(data.duration)}，可随时取消。")

        def work(cancel, report):
            if options.audio_path is not None:
                sources = {-1: Path(options.audio_path)}
            else:
                sources = validate_assets(data, options.video_dir, allow_audio=True)
            asset_key = tuple(sorted((pitch, str(path), path.stat().st_size,
                                      path.stat().st_mtime_ns) for pitch, path in sources.items()))
            key = base_key+(asset_key,)
            if cached_key == key and cached_path is not None and cached_path.is_file():
                return cached_path, key
            path = render_preview_audio(data, options, destination,
                                        on_progress=report, cancel_event=cancel)
            return Path(path), key
        self._start_job("preview", work)

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
        if self._preview_active:
            try:
                playing = self._player.is_playing()
            except (OSError, ValueError) as error:
                self._stop_preview()
                self.status.set("试听播放未完成")
                self._append_log(str(error))
                if not self._closing:
                    messagebox.showerror("无法播放试听", str(error), parent=self)
            else:
                if not playing:
                    self._preview_active = False
                    self.stop_preview_button.configure(state="disabled")
                    if not self._busy and not self._closing:
                        self.status.set("试听结束，可调整选区或导出成片")
        if self._closing and (self._thread is None or not self._thread.is_alive()):
            self.destroy()
            return
        self._poll_after_id = self.after(80, self._poll_events)

    def _finish_job(self, event_kind: str, payload) -> None:
        job_kind = self._job_kind
        self._busy, self._job_kind = False, None
        if self._closing:
            return
        if event_kind == "done" and self._cancel.is_set() and job_kind not in {"export", "audio_export"}:
            if job_kind == "preview":
                self._discard_preview_job()
            self.status.set("任务已取消")
            self._append_log("任务已取消。")
            self._restore_controls()
            return
        if event_kind == "error":
            message, cancelled = payload
            if job_kind == "preview":
                self._discard_preview_job()
            if cancelled:
                self.status.set("任务已取消")
                self._append_log("任务已取消。")
            else:
                self.status.set("任务未完成，请查看提示")
                self._append_log(message)
                messagebox.showerror("操作未完成", message or "发生了未知错误。", parent=self)
        elif job_kind == "read":
            path, data, signature = payload
            self._loaded_path, self._loaded_signature, self._full_data = path, signature, data
            tracks = [track for track in data.tracks if track.note_count > 0]
            self._track_indices = [track.index for track in tracks]
            self.track_list.configure(state="normal")
            for track in tracks:
                name = (track.name or "未命名").replace("\n", " ")[:70]
                self.track_list.insert("end", f"轨道 {track.index+1} · {name}（{track.note_count} 音符）")
            if tracks:
                self.track_list.selection_set(0, "end")
                self._refresh_summary()
                self._refresh_material_table()
                self.status.set("MIDI 已就绪，可调整选区、试听或导出")
                self._append_log(f"读取成功：默认选中 {len(tracks)} 个轨道，全曲 "
                                 f"{_duration_text(data.duration)}。")
            else:
                self.summary.set("这个 MIDI 文件没有可导出的音符。")
                self._set_notes("")
                self.status.set("未找到音符，请更换 MIDI 文件")
            for warning in data.warnings:
                self._append_log(f"提示：{warning}")
        elif job_kind == "check":
            self._material_info.update(payload)
            self._refresh_material_table()
            self.asset_summary.set(f"素材检查通过：已找到当前选区所需的 {len(payload)} 种音高素材。")
            self.status.set("选区素材检查通过，可以试听或导出")
            self._append_log(self.asset_summary.get())
        elif job_kind == "preview":
            path, key = payload
            if self._preview_path is not None and self._preview_path != path:
                self._discard_paths.add(self._preview_path)
                self._clean_stale_previews()
            self._preview_path, self._preview_cache_key = Path(path), key
            self._preview_job_path = None
            try:
                self._player.play(self._preview_path)
            except (OSError, ValueError) as error:
                self._stop_preview()
                self.status.set("试听已生成，但无法播放")
                self._append_log(str(error))
                messagebox.showerror("无法播放试听", str(error), parent=self)
            else:
                self._preview_active = True
                self.progress.configure(value=1)
                self.percent.set("100%")
                self.status.set("正在试听所选片段音频；修改设置会立即停止")
                self._append_log("开始播放完整选区音频。")
        elif job_kind in {"export", "audio_export"}:
            self._last_output = Path(payload)
            self.progress.configure(value=1)
            self.percent.set("100%")
            self.status.set("导出完成")
            kind = "音频" if job_kind == "audio_export" else "视频"
            self._append_log(f"{kind}已保存：{self._last_output}")
            messagebox.showinfo("导出完成", f"{kind}已保存：\n{self._last_output}", parent=self)
        self._restore_controls()

    def _discard_preview_job(self) -> None:
        if self._preview_job_path is not None and self._preview_job_path != self._preview_path:
            self._discard_paths.add(self._preview_job_path)
            self._clean_stale_previews()
        self._preview_job_path = None

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
            self._stop_preview()
            self.status.set("正在停止任务并清理，请稍候…")
            self._restore_controls()
        else:
            self.destroy()

    def destroy(self) -> None:
        for field in ("_poll_after_id", "_cleanup_after_id"):
            timer = self.__dict__.get(field)
            if timer is not None:
                try:
                    self.after_cancel(timer)
                except tk.TclError:
                    pass
                setattr(self, field, None)
        player = self.__dict__.get("_player")
        if player is not None:
            try:
                player.stop()
            except (OSError, ValueError):
                pass
        temporary = self.__dict__.get("_preview_temp")
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                def cleanup_later():
                    for pause in (.1, .3, .6, 1.0):
                        time.sleep(pause)
                        try:
                            temporary.cleanup()
                            return
                        except OSError:
                            pass
                threading.Thread(target=cleanup_later, daemon=True).start()
            self._preview_temp = None
        super().destroy()


def launch_gui(midi_path: str | Path | None = None,
               video_dir: str | Path | None = None,
               output_path: str | Path | None = None) -> None:
    """Prefilling a MIDI example never starts audio playback."""
    app = StitcherApp()
    if midi_path is not None:
        app.midi_path.set(str(midi_path))
        if output_path is None:
            path = Path(midi_path)
            app._suggested_output = str(path.with_name(path.stem+"_stitched.mp4"))
            app.output_path.set(app._suggested_output)
        app.after(100, app._read_midi)
    if video_dir is not None:
        app.video_dir.set(str(video_dir))
    if output_path is not None:
        app.output_path.set(str(output_path))
    app.mainloop()


if __name__ == "__main__":
    launch_gui()

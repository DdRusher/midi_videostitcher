"""Desktop entry point and a scriptable MIDI-to-video command line."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading

VERSION = "0.3.0"


def _channel(value: str) -> int:
    try:
        channel = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("通道必须是 1–16 之间的整数") from exc
    if not 1 <= channel <= 16:
        raise argparse.ArgumentTypeError("通道必须是 1–16 之间的整数")
    return channel - 1


def _size(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x"))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("分辨率格式应为 1280x720") from exc
    if not all(16 <= number <= 7680 and number % 2 == 0 for number in (width, height)):
        raise argparse.ArgumentTypeError("宽高必须是 16–7680 之间的偶数")
    return width, height


def _time(value: str) -> float:
    from selection import parse_time

    try:
        return parse_time(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MIDI 视频拼接：不带参数启动中文窗口，也可通过参数批量导出。"
    )
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--gui", action="store_true", help="打开中文桌面窗口")
    parser.add_argument("--demo", action="store_true", help="生成并载入一套演示 MIDI 和视频素材")
    parser.add_argument("-f", "--f", "--midi", dest="midi", type=Path, help="MIDI 文件路径")
    parser.add_argument("--video-dir", type=Path, default=Path("video"), help="音符视频素材目录（默认 video）")
    parser.add_argument("-o", "--output", type=Path, default=Path("output.mp4"), help="输出 MP4 路径")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--track", type=int, action="append", help="MIDI 轨道索引，从 0 开始，可重复指定；默认第一个含音符轨道")
    selection.add_argument("--all-tracks", action="store_true", help="把全部轨道放在同一时间线上")
    parser.add_argument("--channel", type=_channel, action="append", help="MIDI 通道 1–16，可重复指定；默认全部通道")
    parser.add_argument("--start", type=_time, default=0.0, help="选区开始，秒或 分:秒（默认 0）")
    parser.add_argument("--end", type=_time, help="选区结束，秒或 分:秒；默认曲目结束")
    parser.add_argument("--list-tracks", action="store_true", help="显示轨道、通道和时长后退出")
    parser.add_argument("--check", action="store_true", help="检查素材映射后退出，不渲染")
    parser.add_argument("--size", type=_size, default=(1280, 720), help="输出宽x高（默认 1280x720）")
    parser.add_argument("--fps", type=int, default=30, help="输出帧率（默认 30）")
    parser.add_argument("--chord-policy", choices=("all", "highest", "lowest", "latest"), default="all", help="重叠音符处理，默认 all 同时分屏并叠加原声")
    parser.add_argument("--preview-audio", type=Path, help="将选区原声混音保存为 WAV 试听文件，不渲染视频")
    parser.add_argument("--overwrite", action="store_true", help="允许替换已存在的输出文件")
    parser.add_argument("--ffmpeg", help="自定义 FFmpeg 可执行文件路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(arguments)
    if not arguments or args.gui or args.demo:
        try:
            from gui import launch_gui

            if args.demo:
                from examples.create_demo import create_demo

                midi_path, videos = create_demo()
                launch_gui(midi_path=midi_path, video_dir=videos,
                           output_path=midi_path.parent / "output.mp4")
            else:
                launch_gui()
            return 0
        except Exception as exc:
            details = f"无法启动窗口：{exc}\n请确认已安装项目依赖及 Python 的 Tcl/Tk 组件。"
            print(details, file=sys.stderr)
            try:
                import tkinter as tk
                from tkinter import messagebox

                error_root = tk.Tk()
                error_root.withdraw()
                try:
                    messagebox.showerror("启动失败", details, parent=error_root)
                finally:
                    error_root.destroy()
            except Exception:
                pass
            return 1
    if args.midi is None:
        parser.error("请用 --midi 指定 MIDI 文件，或不带参数打开窗口")
    cancel_event = threading.Event()
    try:
        from notes import load_midi, note_name
        from renderer import RenderOptions, render_midi, render_preview_audio, validate_assets
        from selection import select_midi

        overview = load_midi(args.midi)
        if args.list_tracks:
            print(f"文件：{args.midi}\n时长：{overview.duration:.3f} 秒")
            for track in overview.tracks:
                channels = ", ".join(str(channel + 1) for channel in track.channels) or "无"
                print(f"[{track.index}] {track.name} | 音符 {track.note_count} | 通道 {channels}")
            for warning in overview.warnings:
                print(f"提示：{warning}")
            return 0
        selected_tracks = args.track
        if selected_tracks is None and not args.all_tracks:
            first = next((track.index for track in overview.tracks if track.note_count), None)
            selected_tracks = [first] if first is not None else None
        data = select_midi(overview, tracks=selected_tracks, channels=args.channel,
                           start=args.start, end=args.end)
        for warning in data.warnings:
            print(f"提示：{warning}")
        if args.check:
            mapping = validate_assets(data, args.video_dir)
            for pitch, path in sorted(mapping.items()):
                print(f"{note_name(pitch)} → {path}")
            print(f"检查完成：{len(data.notes)} 个音符，时间线 {data.duration:.3f} 秒")
            return 0
        options = RenderOptions(
            video_dir=args.video_dir,
            output_path=args.output,
            width=args.size[0],
            height=args.size[1],
            fps=args.fps,
            chord_policy=args.chord_policy,
            use_clip_audio=True,
            overwrite=args.overwrite,
            ffmpeg_path=args.ffmpeg,
        )
        progress = lambda value, message: print(f"{value:6.1%}  {message}", flush=True)
        if args.preview_audio:
            result = render_preview_audio(data, options, args.preview_audio,
                                          on_progress=progress, cancel_event=cancel_event)
        else:
            result = render_midi(data, options, on_progress=progress, cancel_event=cancel_event)
        print(f"已保存：{result}")
        return 0
    except KeyboardInterrupt:
        cancel_event.set()
        print("\n已取消导出。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

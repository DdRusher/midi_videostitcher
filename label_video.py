"""Render note/frequency/velocity cards with one continuous video encode."""

from __future__ import annotations

import colorsys
from functools import lru_cache
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font_path() -> str | None:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = (
        windows / "consola.ttf", windows / "arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    )
    return next((str(path) for path in candidates if path.is_file()), None)


@lru_cache(maxsize=128)
def _font(size: int):
    path = _font_path()
    if path is not None:
        return ImageFont.truetype(path, max(8, size))
    try:
        return ImageFont.load_default(size=max(8, size))
    except TypeError:  # Pillow 10.0 fallback on machines without system fonts.
        return ImageFont.load_default()


def _center_text(draw, box, text, size, color):
    left, top, right, bottom = box
    font = _font(size)
    while size > 8 and draw.textbbox((0, 0), text, font=font)[2] > right - left:
        size -= 1
        font = _font(size)
    bounds = draw.textbbox((0, 0), text, font=font)
    x = left + (right - left - bounds[2] + bounds[0]) / 2 - bounds[0]
    y = top + (bottom - top - bounds[3] + bounds[1]) / 2 - bounds[1]
    draw.text((x, y), text, font=font, fill=color)


def _note_card(width, height, voices):
    """Empty MIDI regions stay black; each active note receives one tile."""
    from notes import note_name

    image = Image.new("RGB", (width, height), "black")
    if not voices:
        return image
    columns = math.ceil(math.sqrt(len(voices)))
    rows = math.ceil(len(voices) / columns)
    cell_width, cell_height = width / columns, height / rows
    draw = ImageDraw.Draw(image)
    for index, (pitch, velocity) in enumerate(voices):
        x = int(index % columns * cell_width)
        y = int(index // columns * cell_height)
        right, bottom = int(x + cell_width), int(y + cell_height)
        margin = max(1, min(18, int(min(cell_width, cell_height) * .035)))
        box = (x + margin, y + margin, right - margin, bottom - margin)
        hue = ((pitch % 12) / 12 + .58) % 1
        background = tuple(round(value * 255) for value in colorsys.hsv_to_rgb(hue, .48, .18))
        accent = tuple(round(value * 255) for value in colorsys.hsv_to_rgb(hue, .48, .94))
        draw.rounded_rectangle(box, radius=max(1, margin), fill=background, outline=accent, width=max(1, margin // 3))
        inset = margin * 2
        text_left, text_right = box[0] + inset, box[2] - inset
        inner_height = max(1, box[3] - box[1])
        size = max(8, int(min(cell_width * .3, cell_height * .3)))
        _center_text(draw, (text_left, y + cell_height * .13, text_right, y + cell_height * .44),
                     note_name(pitch), size, "white")
        if cell_width >= 80 and cell_height >= 75:
            frequency = 440 * 2 ** ((pitch - 69) / 12)
            small = max(8, int(min(cell_width * .055, cell_height * .075)))
            _center_text(draw, (text_left, y + cell_height * .49, text_right, y + cell_height * .62),
                         f"{frequency:.3f} Hz", small, accent)
            strength = velocity / 127
            _center_text(draw, (text_left, y + cell_height * .66, text_right, y + cell_height * .79),
                         f"Velocity {velocity}/127", small, "white")
            bar_left, bar_right = box[0] + inset, box[2] - inset
            bar_top = round(box[1] + inner_height * .87)
            bar_bottom = min(box[3] - margin, bar_top + max(2, int(cell_height * .025)))
            if bar_right > bar_left and bar_bottom > bar_top:
                draw.rectangle((bar_left, bar_top, bar_right, bar_bottom), fill=(31, 38, 49))
                if strength > 0:
                    draw.rectangle((bar_left, bar_top, bar_left + (bar_right - bar_left) * strength, bar_bottom), fill=accent)
    return image


def encode_label_video(data, segments, options, destination, work, ffmpeg, cancel_event=None, on_progress=None):
    """Write one PNG per distinct grid, then encode its timed concat once.

    A per-image input frame rate preserves the MIDI's absolute frame clock,
    including single-frame notes. The final duplicate anchors the last span.
    No audio is generated here; the renderer supplies the shared final mix.
    """
    from renderer import _check_cancel, _command, _encode

    directory = Path(work) / "note-cards"
    directory.mkdir()
    images = {}
    lines = ["ffconcat version 1.0"]
    total_frames = segments[-1].end_frame
    for segment in segments:
        _check_cancel(cancel_event)
        key = tuple((voice.pitch, getattr(voice, "velocity", data.notes[voice.note_id].velocity))
                    for voice in segment.voices)
        name = images.get(key)
        if name is None:
            name = f"card-{len(images):06d}.png"
            image = _note_card(options.width, options.height, key)
            image.save(directory / name, compress_level=1)
            image.close()
            images[key] = name
        lines += [f"file '{name}'", f"option framerate {options.fps}",
                  f"duration {segment.frame_count / options.fps:.12f}"]
        if on_progress is not None:
            on_progress(.15 * segment.end_frame / total_frames)
    lines += [f"file '{name}'", f"option framerate {options.fps}"]
    manifest = directory / "cards.ffconcat"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    seconds = total_frames / options.fps
    command = _command(ffmpeg) + [
        "-f", "concat", "-safe", "0", "-i", str(manifest),
        "-vf", f"fps={options.fps}:round=near", "-an", "-frames:v", str(total_frames),
        "-t", f"{seconds:.12f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-bf", "0", "-threads", "2", str(destination),
    ]
    progress = (lambda value: on_progress(.15 + .85 * value)) if on_progress is not None else None
    _encode(command, cancel_event, progress, seconds)

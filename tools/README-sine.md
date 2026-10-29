# 正弦音素材生成器

`generate_sine_assets.py` 使用 Python 标准库生成精确的 PCM 正弦波，再通过 FFmpeg 生成音名、频率和 MIDI 编号画面。它是独立的素材准备工具；Rust 应用本身不需要 Python。

```text
python tools/generate_sine_assets.py --output ../sine_test_MIDI_0_127_5s
```

默认包含 MIDI 0–127，即 C-1 至 G9，共 128 个音高，每个 5 秒。输出目录必须是新目录，避免覆盖已有素材。默认同时编码两个视频，每个编码器使用一个线程。

需要 FFmpeg（包含 `drawtext` 与 `libx264`）可执行文件位于 PATH，也可通过 `--ffmpeg` 指定。自动查找 Windows Consolas、Linux DejaVu Sans Mono、macOS Menlo；可用 `--font` 指定字体文件。

```text
python tools/generate_sine_assets.py --output ../sine_65 --first 25 --last 89
python tools/generate_sine_assets.py --output ../sine_88 --first 21 --last 108
python tools/generate_sine_assets.py --output ../sine_short --duration 2 --workers 2 --ffmpeg C:/ffmpeg/bin/ffmpeg.exe
```

每个素材包含：

- `wav/<音名>.wav`：48 kHz、16 位、双声道 PCM，峰值 20%，两端各 10 ms 淡变。
- `videos/<音名>.mp4`：640×360、30 fps、H.264＋AAC，保留与此前相同的彩色底、白框、中央大音名、频率和 MIDI 编号模板。
- `metadata.json`：音名、编号、精确频率、相对素材路径和格式。
- `tests/test_all_notes.mid`：所有音高依次发声，每音 0.5 秒。
- `tests/test_overlap.mid`：和弦、同音重叠、力度变化与边界音高测试。

在拼接器中选择 `videos` 目录，然后读取测试 MIDI。音名遵循 MIDI 60=C4；频率公式为 `440 × 2^((MIDI−69)/12)`。低于人耳/音箱重现范围的超低音可能听不见。

生成默认 5 秒素材后，可执行完整校验，结果写入素材包的 `tests/asset_verification.json`：

```text
python tools/verify_sine_assets.py ../sine_test_MIDI_0_127_5s
```

校验包括文件数量、音名与频率、PCM 正弦样本、采样帧数、边缘淡变、视频帧数与音视频时长；同时实际解码全部视频并测量 AAC 音高。

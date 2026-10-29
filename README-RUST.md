# Rust 原生版本

这是独立的 Rust 中文桌面程序，包含原生窗口、MIDI 解析、选区与素材安排、音频混合、画面生成和命令行入口。Rust 程序不调用 Python；仓库内原 Python v0.4 源码保留作对照。

实现所用的库、从 MIDI 到成片的步骤、提速原理与复现方法见 [Rust 实现与提速说明](docs/RUST-实现与提速.md)。完整的 128 音高正弦测试素材可以用 [生成脚本](tools/generate_sine_assets.py) 重建。

v0.5.1 改为浅色分步工作台，并修复 Windows 扩展路径导致 FFmpeg 拼接清单找不到中间文件的问题；覆盖已有输出时也使用同一修复。选择正弦素材包的外层目录会自动识别 `videos` 子目录。报错提示可展开并复制完整详情。

## Windows 使用

解压应用包，双击 `start.bat` 或 `native/midi-videostitcher.exe`。不需要安装 Python、NumPy、Pillow 或 Rust 编译器。

程序会查找同目录或 PATH 中的 FFmpeg，也可以在窗口里指定 `ffmpeg.exe`。没有 FFmpeg 时会明确提示；它负责视频编解码与最终音频限幅。

1. 在“导入与选区”选择 MIDI 和素材目录，读取 MIDI，选择需要的轨道与时间区间。素材包可直接选外层目录，程序会识别其中的 `videos` 子目录。
2. 在“素材片段”检查素材后，为每种音高设置固定起点/结束，或随机选段；随机种子不变时试听与导出使用同一批片段。
3. 默认按 MIDI 力度控制原声音量：127 为原音量，64 约一半。可关闭力度控制。
4. 在“声音与输出”选择声音和画面。默认画面显示音名、频率和力度；也可切换为原视频分屏，每个重叠音符独立占一格。
5. 可试听整个选区、单独导出 WAV 或导出 MP4。导入完整音乐时，音乐与原 MIDI 的 0 秒对齐，音名画面无需素材目录。

短素材在所选窗口内循环，同音重复与长音续播保留独立进度。休止保持黑屏；素材原声模式静音，导入整段音乐则继续播放音乐。取消不会替换已有输出。

Windows 试听使用系统声音接口；Linux/macOS 需可用的 FFplay、aplay 或 afplay。程序启动时不播放声音。

## 算法改动

- **音符事件扫描**：MIDI 使用全局速度时钟、跨轨道踏板、同音 FIFO 配对；输出使用统一帧时钟，避免片段累计误差。
- **音频分块并行**：先批量解码唯一素材，再扫描每个输出块当前活动的音符。不同线程只写各自的输出块，保持累加顺序与声部独立性。临时混音顺序写盘，内存不会随全曲时长线性增长。
- **PCM 缓存**：解码后的素材按文件版本与所选窗口缓存，普通素材总缓存约 256 MB；较长素材使用内存映射，预览后导出可复用已解码内容。
- **音名画面**：按音高、力度和网格尺寸复用文字卡片，有限并行生成唯一画面，采用快速 PNG 压缩，整条视频连续编码。
- **视频素材画面**：在缓存预算内，素材只按所需范围与网格尺寸解码，再由 Rust 拷贝像素合成连续帧，一次编码整条视频。预计 RGB 缓存超过约 768 MB 时使用低缓存并行片段模式；磁盘帧缓存目标上限约 1 GB。
- **进程与输出**：FFmpeg 输入直接传参，支持中文路径；取消停止本任务的编码子进程，完成后原子保存，保留输出保护与 FFmpeg 9 参数兼容。

## 编译

安装 [Rust 官方工具链](https://www.rust-lang.org/tools/install)，然后：

```text
cd rust
cargo build --release --locked --bins
cargo test --locked --all-targets
```

Windows 推荐官方 MSVC 工具链及其编译组件。若使用 GNU 工具链，需完整的 MinGW/LLVM 工具；混用 GNU 链接器和 LLVM dlltool 时可能遇到导入表问题，应使用 LLD。应用包不需要这些构建工具。

Linux 需要 FFmpeg、pkg-config、X11、XKB 与 OpenGL 开发库；中文显示还需要系统中文字体，例如 Noto Sans CJK。CI 安装命令见 `.github/workflows/rust.yml`。

只构建命令行核心：

```text
cargo build --release --locked --no-default-features --bin midi-videostitcher-rust
```

## 命令行

```text
midi-videostitcher-rust --midi song.mid --list-tracks
midi-videostitcher-rust --midi song.mid --video-dir videos --visuals labels --output notes.mp4
midi-videostitcher-rust --midi song.mid --video-dir videos --visuals clips --output result.mp4
midi-videostitcher-rust --midi song.mid --video-dir videos --audio-only music.wav
midi-videostitcher-rust --midi song.mid --audio recording.wav --start 30 --end 45 --output excerpt.mp4
midi-videostitcher-rust --midi song.mid --video-dir videos --material-window C4=1,3 --random-material E4 --random-seed 42 --output excerpt.mp4
```

原生 CLI 默认所有轨道；`--track` 与 `--channel` 可重复指定，通道用 1–16。`--info-json` 导出实际选区的解析结果，`--benchmark-json` 记录本次导出耗时与参数。已有输出需要 `--overwrite`。

## 验证

原生测试覆盖 MIDI 时钟/延音/同音重叠、素材窗口与随机复现、并行混音的标量对照、力度音量比、试听缓存与取消、中文窗口小尺寸布局，以及真实 FFmpeg 的原视频续播、独立同音进度、音频对齐与单帧精度。

另外使用真实的 7 分 2 秒 MIDI 作端到端测试：6719 个音符、5640 个画面区间、最多 79 个延音声部。音高、力度、轨道、通道及每个起止时间均与 v0.4 解析结果一致。用户提供的 MIDI、音乐与测试成片不随源码提交。

本机顺序运行的一组全曲测试（1280×720、30 fps、同一套 65 个正弦视频素材、启用力度，同一 FFmpeg、x264 veryfast/CRF 18、两个编码线程）：Python v0.4 音名模式 153.5 秒，Rust 音名模式 55.1 秒，约 2.8 倍速度、耗时减少约 64%。测试时没有同时编译或运行其他导出。两版文字卡片绘制实现不同，因此这是实际功能流程对比，不是单纯语言运算速度对比；实际速度也会随设备与素材变化。

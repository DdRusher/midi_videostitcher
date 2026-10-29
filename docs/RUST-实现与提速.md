# Rust 版用了什么，怎样做

这份说明对应 `codex/rust-engine` 分支。桌面窗口、MIDI 时钟与选区、素材安排、混音和逐帧合成都在 Rust 中实现；FFmpeg 负责解码素材、压缩视频、音频限幅以及封装 MP4。程序运行不需要 Python，也没有用 Python 包装 Rust 窗口。仓库保留的 Python 版用于功能与性能对照；测试素材生成脚本是独立的开发工具。

## 主要组成

| 用途 | 实际使用 | 对应源码 |
| --- | --- | --- |
| 中文桌面窗口 | eframe / egui 0.33.3，Glow 渲染；读取系统中文字体 | `rust/src/gui.rs` |
| 系统文件选择窗口 | rfd | `rust/src/gui.rs` |
| MIDI 文件读取 | midly；本项目实现全局速度时钟、延音踏板、同音符配对与轨道选区 | `rust/src/midi.rs` |
| 音符、时间与画面安排 | Rust 事件扫描、统一帧时钟、固定种子的随机素材窗口 | `rust/src/timeline.rs` |
| 并行计算 | Rayon 的线程池与并行迭代，限制同时处理的任务数 | `rust/src/audio.rs`、`video.rs` |
| 音频 | FFmpeg 解码到 48 kHz 双声道；Rust 浮点采样混音；hound 写 WAV | `rust/src/audio.rs` |
| 大素材读取 | memmap2 只读内存映射，避免把所有原始帧复制到堆内存 | `rust/src/audio.rs`、`video.rs` |
| 音名画面 | image、ab_glyph、font8x8；绘制音名、Hz、力度和力度条 | `rust/src/video.rs` |
| 缓存与临时文件 | blake3 生成缓存键，tempfile 管理任务目录，完成后保存输出 | `rust/src/video.rs`、`engine.rs` |
| 命令行与参数记录 | clap、serde / serde_json | `rust/src/main.rs`、`model.rs` |

准确的依赖版本固定在 `rust/Cargo.lock`，并不是每次运行都临时下载。

## 从 MIDI 到成片

1. **把节拍换成真实秒数。** 依照 MIDI 中的速度变化计算每个音符的开始、结束、力度、通道和轨道。延音踏板按通道生效；同音符多次按下仍保留多个独立声部。
2. **应用选区。** 选择轨道、通道和起止时间；选区从长音中间开始时，保留素材已经播放到哪里，而不是重新触发开头。
3. **匹配素材和确定取材范围。** 例如 MIDI 60 匹配 `C4.mp4` 或 `60.mp4`，MIDI 61 可以匹配 `C#4.mp4` 或 `Db4.mp4`。每个音高可固定取材范围或随机起点；同一随机种子让试听与导出复现同一次安排。
4. **混合原声。** 素材解码后按音符时间放入混音；每个声部乘以 `velocity / 127`，重叠声部直接相加，最终限幅。导入完整音乐时改为截取该音乐对应的时间段，音乐不足的部分补静音。
5. **合成画面。** 音名模式把活动声部画成音名/频率/力度卡片；原视频模式为每个活动音符放一格。网格改变时，长音按原进度继续播放。相同音高的重叠声部也分别显示。
6. **编码与保存。** 输出使用统一帧编号，避免每段小数秒累加造成漂移。任务完成后再保存到目标文件；取消或失败保留原来已有的输出。

## 为什么会快

主要改进是减少重复工作，而不只是换语言。

- **素材重复音符共享解码结果。** 同一文件和窗口的 PCM 可在试听与导出之间复用；文件大小、修改时间或窗口变化会使缓存失效。普通 PCM 缓存目标上限为 256 MB，单份较大的 PCM 使用内存映射。
- **按输出块混音。** 每块 16,384 个采样帧，只处理当前时间真正发声的音符。不同块可以并行，各线程只写自己的输出区域，保留确定的声部累加顺序。临时混音顺序写盘。
- **复用卡片。** 音高、力度、格子大小相同时复用已绘制文字，重复组合也复用画面，再连续编码整条视频。
- **原视频连续合成。** 在预算内，把需要的素材范围按网格尺寸解码为 RGB，再由 Rust 合成连续帧，一次编码。复杂曲目所需缓存超预算时，切换为有界并行片段方式。不会给每个音符无上限地开启一个编码进程。
- **限制资源使用。** 工作线程有上限，较大的数据使用磁盘与映射；避免通过无限堆内存或无限并发换取短暂速度。

Rayon 的并行迭代与线程池用法见 [Rayon 官方文档](https://docs.rs/rayon/latest/rayon/)。窗口框架见 [eframe 源码与说明](https://github.com/emilk/egui/tree/main/crates/eframe)，媒体工具见 [FFmpeg 官方文档](https://ffmpeg.org/documentation.html)。

## 实际测过什么

在同一台电脑上，用用户提供的约 7 分 2 秒《月光》MIDI、65 个正弦视频素材，顺序执行完整流程：1280×720、30 fps、启用力度、同一 FFmpeg、x264 veryfast / CRF 18、两个编码线程。Python v0.4 音名模式耗时 153.5 秒，Rust 音名模式耗时 55.1 秒，约为 2.8 倍速度；这是这台电脑上的一次实测，不是所有机器或所有模式的固定倍数。

6719 个音符的音高、力度、通道、轨道及起止时间均与 Python 对照一致，输出两版均为 12,656 帧。两版成片解码为 48 kHz 双声道 PCM 后的音频 SHA-256 完全相同。两版文字绘制实现不同，这组数据比较的是实际处理流程。原视频模式的速度还取决于素材尺寸、运动量、重复组合和可用缓存，不能直接套用音名模式的倍数。

自动测试还检查固定/随机窗口、力度音量比、同音重叠、长音续播、导入音乐对齐、输出保护、取消任务和小窗口布局。用户 MIDI 与实际测试成片只在本地保留，不随源码上传。

## 自己编译和复现

准备 Rust 工具链与 FFmpeg。Windows 推荐 MSVC 工具链及其构建组件；Linux 开发库安装步骤见 `.github/workflows/rust.yml`。

```text
cd rust
cargo test --locked --all-targets
cargo build --release --locked --bins
```

窗口入口为 `target/release/midi-videostitcher.exe`，命令行入口为 `target/release/midi-videostitcher-rust.exe`。运行成品不需要 Rust 编译器。

```text
midi-videostitcher-rust --midi song.mid --video-dir videos --visuals clips --output result.mp4 --benchmark-json benchmark.json
midi-videostitcher-rust --midi song.mid --video-dir videos --audio-only music.wav
```

需要重建正弦素材时，使用仓库的 `tools/generate_sine_assets.py`。MIDI 编号 0–127 对应 C-1–G9，采用 A4 = 440 Hz、十二平均律，公式为 `f = 440 × 2^((midi - 69)/12)`。每个音符 5 秒，分别提供原始 WAV 与带音名、频率画面的 MP4；素材包附带频率表和测试 MIDI。

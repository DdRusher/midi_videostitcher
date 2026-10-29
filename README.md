# MIDI 视频拼接器 · Rust 分支

本分支提供 **Rust 原生中文桌面程序、MIDI 解析、并行音频混合与视频合成**，运行不需要 Python。Windows 应用包解压后双击 `start.bat` 或 `native/midi-videostitcher.exe`；源码构建、算法和测试说明见 [README-RUST.md](README-RUST.md)。

Rust 源码位于 `rust/`，支持原声分屏、音名/频率/力度画面、固定与随机素材选段、选区试听、WAV 导出及导入完整音乐。FFmpeg 负责媒体编解码。

下方保留 v0.4 Python 实现的参考文档，供回归与性能比较；其启动器改为 `start-python.bat`。

## Python v0.4 参考文档

把每个音符对应的素材，按 MIDI 的真实时间线编排成 MP4 或 WAV。**重叠音符同时分屏，原声按 MIDI 力度叠加。** v0.4 支持每个素材固定/随机选段、单独导出音频，以及“已有 MIDI＋导入音频”的快速音名/频率/力度画面。

## Windows：双击启动

需要 **Python 3.10 或更新版本**。安装 Python 时请保留 Tcl/Tk，并勾选 **Add Python to PATH**。

下载或克隆本仓库后，双击 **start-python.bat**。首次启动在项目目录建立独立环境并安装依赖，需要联网；以后直接打开窗口。

双击 **start_demo.bat** 可以先体验：程序会生成含变速、重叠音符、同音多次重叠和多轨道的演示 MIDI，以及三段带原声音效的彩色视频。点击“试听选区原声”或“开始导出”即可检验效果。演示文件存放在当前项目的 midi_videostitcher_demo 目录。

## 窗口操作

1. **选择文件**：选择 MIDI、视频素材目录和 MP4 保存位置，点击“读取 MIDI”。
2. **选择轨道**：有音符的轨道默认全选，点击列表可逐项切换；也可“全选”或“清空”。按需限定 MIDI 通道。
3. **选择片段**：在音符时间线上拖动两个边界或拖出一个新范围，也可输入起止时间。支持秒数、分:秒、时:分:秒；结束留空表示全曲结束。“选择全曲”可重置。
4. **设置素材片段**：点击“检查素材”读取时长，在素材列表选择音高，填写允许使用的起止时间，选择“固定起点”或“随机选段”，应用到选中素材；也可批量应用。
5. **选择声音与画面**：声音可用素材原声，或导入整段音频。窗口默认快速生成音名、频率和力度卡片，也可切换为视频素材分屏。导入音频时自动选择卡片画面，无需视频素材。
6. **试听与导出**：试听整个选区，可停止或取消。点击“仅导出音频”保存 WAV，或“开始导出”保存 MP4。

试听、音频导出和视频导出使用相同的选区、随机种子、素材偏移及音量规则。MIDI 负责时间和音符信息；不会从导入音频自动识别 MIDI，也不会合成缺失的原声。

## 每个素材怎样选段

- 例如原素材 5 秒，音符需要 2 秒：固定选段设为开始 1、结束 3，就使用这一段；设为随机、范围 0–5，则为每次音符随机选取能容纳其时长的起点。
- 结束留空表示素材结尾。需要时可把范围限制为 1–4，只在这段内固定或随机取材。
- 允许区间比音符短时，只在所选区间内部循环，不会播放到区间以外。
- 长音遇到其他音符进入/退出、或从长音中途截取 MIDI 选区时，仍接着原素材进度播放。
- 同一 MIDI 选区、素材设置和随机种子得到相同结果；试听与导出一致。点击“重新随机”才主动换一批结果。改变轨道或时间范围也可能改变随机分配。
- 表格同时列出素材总时长和当前 MIDI 选区的最长音符，便于判断需要截多长。

## 力度、单独音频与导入音乐

- 默认启用 MIDI 力度：每个音符的原声音量乘以 velocity/127。127 为原音量，64 约一半，32 约四分之一。可关闭该选项，恢复固定音量。
- 音名画面显示音名、Hz、Velocity 数值和力度条；MIDI 概览也显示选区力度范围。
- “仅导出音频”生成 48 kHz、16 位、双声道 WAV，不渲染视频。素材目录可以直接使用同名 WAV、FLAC、MP3、M4A、OGG 等音频素材。
- “导入整段音频”使用已有音乐替换素材混音；音乐和 MIDI 的原始 0 秒对齐，选择 MIDI 的 30–45 秒时，也截取音乐对应的 30–45 秒。导入音乐不会再受每个 MIDI 音符的力度调节，画面仍显示 MIDI 力度。
- 导入音频比选区短时，余下部分补静音；导入音频不会自动循环。

## 导出速度

原声先按素材批量解码，再用数组混音，重复音符无需反复解码；素材信息按文件版本缓存。视频素材画面自动并行处理，长音续播只从循环区间内的当前进度读取。

**音名/频率/力度画面更快**：重复网格复用图片，整条视频只编码一次。只需要听音乐和查看音符时，优先选择这个模式，或直接导出 WAV。

本机一次对比（100 个音符、103 个网格片段、最多 4 声部，20.6 秒，640×360/30fps）：v0.3 视频素材模式约 16.1 秒，v0.4 视频素材模式约 7.3 秒，音名画面约 2.0 秒。这是单次本机测试，实际速度取决于素材、分辨率、机器和后台负载。

## 重叠与原声怎样处理

- 默认“同时分屏＋原声叠加”：2 个同时音符左右分屏，3–4 个为 2×2，更多按自动网格显示。每个活动音符都占一格，包含同音高的多次重叠。
- 随重叠数变化，网格会自动重排；长音的视频和原声从原播放进度继续，不会因重排从头重播。
- 各声部原声先应用各自 MIDI 力度，再直接相加，不按人数平均压低；最终限制峰值避免爆音。
- 如果只想跟随一条音符，也可以改选“最高音”“最低音”或“最后进入的音符”。
- 从一个长音的中间截取选区时，视频及原声也从相应素材进度开始。
- 休止保留为黑屏；素材原声模式同时静音，导入整段音乐则继续播放音乐。前奏和尾部不会丢失。短素材在所选区间内循环。
- 素材没有音轨时，该声部保持静音并给出提示；如果选区所有素材都无声音，会明确提示无法试听。

## 素材命名

每个音高提供一段对应的视频，放在同一目录：

~~~text
video/
  C4.mp4
  C#4.mp4
  E4.mp4
  G4.mp4
~~~

- **MIDI 60 = C4**。部分音乐软件把中央 C 标成 C3，请以 MIDI 数字为准。
- 也支持数字名 60.mp4，或降号名 Db4.mp4。
- 支持 .mp4、.mov、.mkv、.webm、.avi 和 .m4v。
- 同一音高有多个候选（例如 C4.mp4 与 60.mov）时会提示歧义，请只保留一份。
- 视频素材自动等比缩放并补黑边，多格画面有细黑边便于区分；音名画面按相同重叠规则分格。
- 这是按音高选择对应素材的工具；需要每个音高的原声素材，不会把一段视频自动变调成其他音高。

## 安装与手动启动

~~~bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py

# macOS / Linux
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
~~~

Linux 如缺少 tkinter，请安装发行版的 Python Tk 包，例如 python3-tk。依赖包含 FFmpeg，也可使用 PATH 中已有的 FFmpeg。Windows 试听使用系统声音接口；macOS / Linux 试听需要可用的 FFplay、afplay 或 aplay。没有播放器时仍可导出 WAV 后自行播放。

## 命令行

不带参数打开中文窗口；旧的 --f 参数仍可使用。轨道索引从 0 开始，通道使用 1–16。

~~~bash
# 查看轨道
python main.py --midi song.mid --list-tracks

# 默认首个含音符轨道，原声分屏导出
python main.py --f song.mid --video-dir video --output result.mp4

# 多轨道、多通道，截取 30–45 秒，只检查选区素材
python main.py --midi song.mid --track 1 --track 2 --channel 1 --channel 2 --start 00:30 --end 00:45 --video-dir video --check

# 全部轨道，导出选区原声为 WAV，供试听
python main.py --midi song.mid --all-tracks --start 12.5 --end 20 --video-dir video --preview-audio preview.wav

# 每个音符按 MIDI 力度混音，只导出音乐；也可将 video 换成同名 WAV 素材目录
python main.py --midi song.mid --all-tracks --video-dir video --audio-only music.wav

# C4 固定使用 1–3 秒，E4 随机选段；相同种子可复现结果
python main.py --midi song.mid --video-dir video --material-window C4=1,3 --random-material E4 --random-seed 42 --output excerpt.mp4

# 所有素材随机选段，快速音名/频率/力度画面，保留素材原声
python main.py --midi song.mid --all-tracks --video-dir video --random-all --visuals labels --output notes.mp4

# 已有 MIDI＋导入完整音乐，无需素材目录；选区自动截取对应音乐
python main.py --midi song.mid --all-tracks --audio recording.wav --start 30 --end 45 --output music_notes.mp4

# 1080p / 60fps，所有同时音符分屏＋原声叠加
python main.py --midi song.mid --all-tracks --video-dir video --output result.mp4 --size 1920x1080 --fps 60 --chord-policy all

# 演示窗口 / 无窗口生成完整演示
python main.py --demo
python examples/create_demo.py --output-dir examples/polyphony_demo --render
~~~

重复 --track 或 --channel 表示组合选择；通道与轨道条件取交集。GUI 默认全选音符轨道，CLI 默认第一条旋律轨道，可用 --all-tracks 改为全部。

已有输出需要 --overwrite；GUI 会询问是否覆盖。导出和试听生成都可取消，未完成时保留原输出并清理临时文件。支持 --ffmpeg 指定可执行文件；--workers 1 可关闭视频素材并行处理，默认 0 自动。--ignore-velocity 可关闭力度音量，--audio-offset 可额外偏移导入音乐的起点。更多选项见 --help。

## 时间精度与支持范围

- 支持 Type 0/1 MIDI、跨轨速度信息、中途变速、同音重叠、velocity 0 的 note_on、延音踏板及轨道/通道筛选。
- 选区音符精确裁到边界，并保留选区本身的休止。
- 输出按统一帧时钟对齐；不足一帧的音符可能无法显示，会给出提示。提高帧率可呈现更短的音符。
- 总时长向上对齐到完整一帧，不会逐片段累积误差；试听与视频使用同一时间规则。
- 暂不支持异步 Type 2 或 SMPTE 时间格式，会明确提示。

## 开发与测试

~~~bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
~~~

测试覆盖 MIDI 变速/延音、固定/随机素材窗口与续播、力度音量比、多格画面、音名卡片单帧精度、多声部混音连续性、导入音乐对齐、试听与音视频导出一致性、取消和输出保护，以及中文窗口的选择、试听停止及后台状态。测试不会向扬声器发声；无图形环境时跳过 Tk 测试。

也可安装为命令：

~~~bash
python -m pip install .
midi-videostitcher
midi-videostitcher-gui
~~~

## 常见问题

- **素材缺失**：按选区列出的音名补齐视频，或调整所选轨道、通道和时间范围。
- **听不到某个声部**：检查对应素材本身是否带音轨。MIDI 不是录音，软件不会补造缺失原声。
- **无法播放试听**：先检查系统声音输出设备；非 Windows 系统还需安装上文列出的播放器。
- **无法打开窗口**：确认 Python 有 Tcl/Tk，在终端运行 python main.py 查看错误。
- **导出慢**：只需要音乐时用“仅导出音频”；需要音符画面时选“音名 / 频率 / 力度（快速）”。使用原视频时先试短选区、480p 或 720p，重叠声部多或分辨率高会增加处理时间。
- **更新后仍看到旧界面**：关闭原窗口，再运行 start.bat。

## 许可

MIT，见 LICENSE。运行、试听和渲染均在本机完成。

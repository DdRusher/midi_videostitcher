# MIDI 视频拼接器

把每个音符对应的视频素材，按 MIDI 的真实时间线编排成 MP4。**重叠的音符会同时分屏显示，各自的视频原声同时叠加。** 提供中文桌面窗口、选区试听及命令行入口。

## Windows：双击启动

需要 **Python 3.10 或更新版本**。安装 Python 时请保留 Tcl/Tk，并勾选 **Add Python to PATH**。

下载或克隆本仓库后，双击 **start.bat**。首次启动在项目目录建立独立环境并安装依赖，需要联网；以后直接打开窗口。

双击 **start_demo.bat** 可以先体验：程序会生成含变速、重叠音符、同音多次重叠和多轨道的演示 MIDI，以及三段带原声音效的彩色视频。点击“试听选区原声”或“开始导出”即可检验效果。演示文件存放在当前项目的 midi_videostitcher_demo 目录。

## 窗口操作

1. **选择文件**：选择 MIDI、视频素材目录和 MP4 保存位置，点击“读取 MIDI”。
2. **选择轨道**：有音符的轨道默认全选，点击列表可逐项切换；也可“全选”或“清空”。按需限定 MIDI 通道。
3. **选择片段**：在音符时间线上拖动两个边界或拖出一个新范围，也可输入起止时间。支持秒数、分:秒、时:分:秒；结束留空表示全曲结束。“选择全曲”可重置。
4. **检查素材**：只核对选中的轨道、通道和时间区间实际用到的音高。
5. **试听原声**：后台生成整个选区的原声混音后播放。可取消生成或停止播放；调整选区或设置会停止旧试听。试听不会自动截成前几秒。
6. **导出**：选择分辨率、帧率和重叠处理方式，点击“开始导出”。完成后可打开成片或目录。

试听和导出使用相同的素材原声、选区裁剪与混音规则。MIDI 负责安排时间和选片，不合成乐器音色；界面没有外部配乐输入。

## 重叠与原声怎样处理

- 默认“同时分屏＋原声叠加”：2 个同时音符左右分屏，3–4 个为 2×2，更多按自动网格显示。每个活动音符都占一格，包含同音高的多次重叠。
- 随重叠数变化，网格会自动重排；长音的视频和原声从原播放进度继续，不会因重排从头重播。
- 各声部原声直接相加，不按人数把每一路平均压低；最终限制峰值避免爆音。
- 如果只想跟随一条音符，也可以改选“最高音”“最低音”或“最后进入的音符”。
- 从一个长音的中间截取选区时，视频及原声也从相应素材进度开始。
- 休止保留为黑屏和静音，前奏和尾部不会丢失。短素材会循环播放。
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
- 自动等比缩放并补黑边，多格画面有细黑边便于区分。
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

# 1080p / 60fps，所有同时音符分屏＋原声叠加
python main.py --midi song.mid --all-tracks --video-dir video --output result.mp4 --size 1920x1080 --fps 60 --chord-policy all

# 演示窗口 / 无窗口生成完整演示
python main.py --demo
python examples/create_demo.py --output-dir examples/polyphony_demo --render
~~~

重复 --track 或 --channel 表示组合选择；通道与轨道条件取交集。GUI 默认全选音符轨道，CLI 默认第一条旋律轨道，可用 --all-tracks 改为全部。

已有输出需要 --overwrite；GUI 会询问是否覆盖。导出和试听生成都可取消，未完成时保留原输出并清理临时文件。支持 --ffmpeg 指定可执行文件，更多选项见 --help。

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

测试覆盖 MIDI 变速/延音、选区与素材续播、多格像素位置、多声部频率与音量、同音多次重叠、混音连续性、限幅器延迟补偿、试听与导出一致性、取消和输出保护，以及中文窗口的选择、试听停止及后台状态。测试不会向扬声器发声；无图形环境时跳过 Tk 测试。

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
- **导出慢**：先用短选区和较低分辨率试导出。重叠声部多、音符多或分辨率高都会增加处理时间。
- **更新后仍看到旧界面**：关闭原窗口，再运行 start.bat。

## 许可

MIT，见 LICENSE。运行、试听和渲染均在本机完成。

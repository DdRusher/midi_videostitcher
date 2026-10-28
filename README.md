# MIDI 视频拼接器

把每个音符对应的视频素材，按 MIDI 的真实时间线编排成 MP4。提供中文桌面窗口，也保留命令行接口。

## 开始使用

需要 **Python 3.10 或更新版本**。Windows 的 Python 安装程序中请保留 Tcl/Tk，并勾选 **Add Python to PATH**。

### Windows：双击启动

下载或克隆本仓库，双击 **start.bat**。第一次启动会在项目目录建立独立的运行环境并安装依赖，需要联网；以后直接打开窗口。

想先看效果，双击 **start_demo.bat**。它会生成一份原创测试 MIDI、三个带音高音效的彩色视频，并自动填入窗口。点击“检查素材”，再点击“开始导出”即可体验。

### 手动启动 / macOS / Linux

```bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py

# macOS / Linux
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Linux 如缺少 tkinter，请安装发行版提供的 Python Tk 包（例如 python3-tk）。安装包提供 FFmpeg；也可以使用 PATH 中已有的 FFmpeg。项目不再依赖 MoviePy。

## 窗口操作

1. 选择 MIDI 文件，点击“读取 MIDI”。
2. 选择含旋律的轨道，按需限定通道。通常先选一条旋律轨道即可。
3. 选择素材目录。窗口会列出所选轨道需要的音名，点击“检查素材”核对。
4. 选择 MP4 保存位置，设置分辨率、帧率与和弦取音规则。
5. 如有完整歌曲的音频，可以选择“配乐”；否则默认使用素材原声。
6. 点击“开始导出”。导出期间可以取消，完成后可打开成片或所在目录。

导出不会阻塞窗口。已有输出需要确认覆盖；编码成功之前不会替换旧文件。取消时会终止编码并清理临时文件。

## 素材怎样命名

每个音高提供一个对应视频，放在同一个目录，例如：

```text
video/
  C4.mp4
  C#4.mp4
  D4.mp4
  E4.mp4
  G4.mp4
```

- 音名标准：**MIDI 60 = C4**。一些音乐软件把中央 C 标成 C3，请以 MIDI 数字为准。
- 也可以用 MIDI 数字命名，如 60.mp4；降号别名如 Db4.mp4 也支持。
- 支持 .mp4、.mov、.mkv、.webm、.avi 和 .m4v。
- 同一音高出现多个候选（如 C4.mp4 和 60.mov）会报歧义，请只保留一个。
- 素材不足音符时长时自动循环；尺寸不同会等比缩放并补黑边。
- 软件按音符选择对应素材，**不会把一段素材自动变调成其他音高**。

## 时间与声音

- 支持跨轨道的速度信息、中途变速、前奏、音符间休止及尾部时长。
- 支持同音重叠、力度为 0 的 note_on、延音踏板和轨道/通道筛选。
- 休止使用黑屏和静音，不会把休止删掉后导致后面的片段提前。
- 和弦/重叠音符每个时刻显示一个视频：最高音、最低音或最后进入的音符。当前版本不做多画面拼贴，也不混合多个音符视频的声音。
- MIDI 是演奏事件，不含录音；未提供带声音的素材或外部配乐时，输出不会自动合成乐器音色。
- 配乐从时间线起点播放，过长截断、过短补静音；应选择与 MIDI 对齐的录音。
- 视频按帧对齐时间；极短音符可能无法占据一帧，会在导出时提示。需要更细的切换可提高帧率。总时长向上对齐到完整一帧，不会逐片段累积误差。
- 不支持异步 Type 2 MIDI 或 SMPTE 时间格式，会明确提示。

## 命令行

不带参数打开窗口；旧的 --f 参数仍可使用。轨道索引从 0 开始，通道用 1–16。

```bash
# 查看轨道信息
python main.py --midi song.mid --list-tracks

# 只检查素材，不导出
python main.py --midi song.mid --track 1 --video-dir video --check

# 导出；默认选择第一个含音符的轨道，默认 720p / 30fps
python main.py --f song.mid --video-dir video --output result.mp4

# 指定轨道、通道和外部配乐
python main.py --midi song.mid --track 1 --channel 1 --video-dir video --audio song.wav --output result.mp4 --size 1920x1080 --fps 60

# 显式允许覆盖，并选择重叠音符策略
python main.py --midi song.mid --video-dir video --chord-policy highest --overwrite

# 生成演示并打开预填好的窗口
python main.py --demo

# 无窗口生成演示素材及成片
python examples/create_demo.py --output-dir examples/demo --render
```

也支持 --all-tracks、--silent 和 --ffmpeg。用 python main.py --help 查看全部选项。

## 开发与验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用临时生成的 MIDI 和视频，覆盖变速、休止、同音重叠、延音、筛选、素材缺失/歧义、短片循环、真实成片画面与音频、取消、旧输出保护及中文路径。图形界面也可单独通过 python main.py --demo 检查。CI 在 Windows 和 Linux 上运行测试。

可选安装为命令：

```bash
python -m pip install .
midi-videostitcher
midi-videostitcher-gui
```

## 常见问题

- **素材检查缺少音符**：按窗口列出的音名补齐视频，或改选正确的轨道/通道。
- **没有声音**：确认素材自身有音轨，或选择外部配乐；MIDI 文件本身不是音频。
- **FFmpeg 启动失败**：重新安装 requirements.txt 中的 imageio-ffmpeg，或用命令行 --ffmpeg 指定已有的可执行文件。
- **无法打开窗口**：确认 Python 安装了 tkinter；在命令行运行 python main.py 查看完整错误。
- **导出慢**：先选较低分辨率或较低帧率试导出。当前按时间片段编码，音符数量很多时会较慢。

## 许可

MIT，见 LICENSE。运行和渲染均在本机完成。

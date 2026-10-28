@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 正在准备运行环境，首次启动可能需要一点时间...
    python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if errorlevel 1 goto needpython
    python -m venv ".venv"
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo Python 缺少 Tcl/Tk 组件，请重新运行 Python 安装程序添加该组件。
    goto failed
)
".venv\Scripts\python.exe" -c "import mido, imageio_ffmpeg" >nul 2>&1
if errorlevel 1 (
    echo 正在安装运行所需组件...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto failed
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py" %*
exit /b 0
:needpython
echo 请先安装 Python 3.10 或更新版本，并勾选 Add Python to PATH。
echo 安装时请保留 Tcl/Tk 组件，然后重新打开本文件。
pause
exit /b 1
:failed
echo 启动失败。请把上面的错误信息发给开发者，或查看 README.md 的排查说明。
pause
exit /b 1

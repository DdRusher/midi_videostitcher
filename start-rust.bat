@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist "native\midi-videostitcher.exe" (
    start "" "native\midi-videostitcher.exe"
    exit /b 0
)
if exist "rust\target\release\midi-videostitcher.exe" (
    start "" "rust\target\release\midi-videostitcher.exe"
    exit /b 0
)
echo 请使用包含 EXE 的 Rust 版下载包，或按 README-RUST.md 编译。
echo Rust 版本运行不需要 Python。
pause
exit /b 1

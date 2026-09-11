@echo off
chcp 65001 >nul
REM ============================================================
REM  mi_health · token 刷新 (双击即可)
REM  自动使用 AstrBot 的 .venv 环境, 调用 refresh_token.py 扫码登录
REM ============================================================

setlocal

REM 脚本所在目录 (=<AstrBot 根>/data/plugins/mi_health/tools)
set "SCRIPT_DIR=%~dp0"

REM 向上定位到 AstrBot 根目录
pushd "%SCRIPT_DIR%..\..\..\.."
set "ASTRBOT_ROOT=%CD%"
popd

set "PYTHON_EXE=%ASTRBOT_ROOT%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [错误] 找不到 AstrBot 虚拟环境: %PYTHON_EXE%
    echo         请确认 AstrBot 已经用 .venv 安装
    pause
    exit /b 1
)

echo 使用 Python: %PYTHON_EXE%
echo.

"%PYTHON_EXE%" "%SCRIPT_DIR%refresh_token.py"

echo.
echo ============================================================
echo  执行完毕, 按任意键关闭窗口
echo ============================================================
pause >nul
endlocal

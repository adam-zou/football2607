@echo off
setlocal EnableExtensions

chcp 65001 >nul
cd /d "%~dp0"

set "APP_PYTHON="
set "APP_PYTHON_ARGS="

if exist ".venv\Scripts\python.exe" (
    set "APP_PYTHON=%CD%\.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "APP_PYTHON=py"
        set "APP_PYTHON_ARGS=-3"
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 set "APP_PYTHON=python"
    )
)

if not defined APP_PYTHON (
    echo [错误] 未找到 Python 3。
    echo 请先安装 Python 3，或在项目根目录创建 .venv 虚拟环境。
    pause
    exit /b 1
)

if not exist "SimpleCrawler\run_scheduler.py" (
    echo [错误] 未找到 SimpleCrawler\run_scheduler.py。
    pause
    exit /b 1
)

if not exist "MatchWeb\server.py" (
    echo [错误] 未找到 MatchWeb\server.py。
    pause
    exit /b 1
)

echo 正在启动 SimpleCrawler 调度器和 MatchWeb 网页应用...
echo 调度监控: http://127.0.0.1:8081/
echo 网页应用: http://127.0.0.1:8082/
echo.
echo 两个服务会分别在新窗口中运行；关闭对应窗口即可停止服务。

start "Football2607 - Scheduler" cmd /k ""%APP_PYTHON%" %APP_PYTHON_ARGS% "%CD%\SimpleCrawler\run_scheduler.py""
start "Football2607 - MatchWeb" cmd /k ""%APP_PYTHON%" %APP_PYTHON_ARGS% "%CD%\MatchWeb\server.py""

endlocal

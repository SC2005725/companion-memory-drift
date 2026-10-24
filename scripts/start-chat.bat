@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   启动「小满」聊天
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo   [出错] 找不到 python 命令。
    echo.
    echo   你的 Python 装在这里：
    echo     %%LOCALAPPDATA%%\Programs\Python\Python312\python.exe
    echo   如果上面那个文件存在，说明只是没加进 PATH，请告诉助手。
    echo.
    pause
    exit /b 1
)

python src\main.py
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
    echo ============================================================
    echo   程序退出，代码 %EXITCODE%（非 0 表示出了点问题）
    echo ============================================================
    echo   把上面最后几行红字/报错拍给助手看。
) else (
    echo 已退出。这次聊天已经全部落盘。
)
echo.
pause

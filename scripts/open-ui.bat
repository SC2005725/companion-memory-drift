@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   打开「小满」界面
echo ============================================================
echo.
echo   浏览器会自动打开。关掉这个黑窗口就等于退出程序。
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo   [出错] 找不到 python 命令。
    echo.
    echo   你的 Python 装在这里：
    echo     %LOCALAPPDATA%\Programs\Python\Python312\python.exe
    echo   如果上面那个文件存在，说明只是没加进 PATH，请告诉助手。
    echo.
    pause
    exit /b 1
)

echo   正在启动…（第一次可能要等两三秒）
echo.
python src\main.py ui
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
    echo ============================================================
    echo   启动失败，代码 %EXITCODE%
    echo ============================================================
    echo   把上面最后几行拍给助手看。
    echo.
    echo   临时排查：也可以直接双击 scripts/start-chat.bat 用命令行版本。
)
echo.
pause

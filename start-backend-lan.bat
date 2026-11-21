@echo off
rem 2026-11-21: LAN access for phone APK. Binds 0.0.0.0:8901, no browser.
rem Security relies on webui four-check auth (token + Host/Origin allowlist),
rem not on loopback binding. Desktop shell (desktop.py) now uses the same args.
cd /d "%~dp0src"
start "" pythonw.exe webui.py --port 8901 --host 0.0.0.0 --no-open
echo Heci backend started on 0.0.0.0:8901 (detached).

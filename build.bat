@echo off
cd /d "%~dp0."
py -3 -m PyInstaller --noconfirm --clean --windowed --uac-admin --name RescueWorkbench --add-data "web;web" --add-data "engine;engine" --add-data "licenses;licenses" --hidden-import sfadapter --hidden-import pipeline --hidden-import jobstore --hidden-import stability --hidden-import imager --hidden-import mapfile --hidden-import safeguard app.py
echo.
echo === DONE: dist\RescueWorkbench\RescueWorkbench.exe ===

@echo off
rem mcuVG converter for Windows: double-click to open the window, or drop SVG files on this file.
cd /d "%~dp0"
python tools\mcuvg_gui.py %*
if errorlevel 1 pause

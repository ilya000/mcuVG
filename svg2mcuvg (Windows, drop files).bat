@echo off
rem mcuVG: drop SVG files on this file to convert them next to themselves (no window).
python "%~dp0tools\svg2mcuvg.py" --lua %*
pause

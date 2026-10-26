#!/bin/bash
# mcuVG converter for macOS: double-click to open the converter window (needs Python 3 with tkinter).
cd "$(dirname "$0")" && exec python3 tools/mcuvg_gui.py "$@"

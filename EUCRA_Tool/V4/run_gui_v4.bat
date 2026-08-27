@echo off
cd /d "%~dp0"
python pqc_openssl_gui_v4.py
if errorlevel 1 pause

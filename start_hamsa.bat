@echo off
rem Double-click to host Hamsa on the local Wi-Fi. Keep the window open while the clinic uses it.
title Hamsa Homeopathy server
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_lan_server.ps1"
pause

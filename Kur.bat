@echo off
title BudsTrack
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
timeout /t 6 >nul

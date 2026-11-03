@echo off
title Chess Insights Builder
cd /d "%~dp0"
set "PY="
rem Prefer real Python installs; skip the Microsoft Store "python" shortcut.
if exist "%USERPROFILE%\miniforge3\python.exe" set "PY=%USERPROFILE%\miniforge3\python.exe"
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY if exist "%USERPROFILE%\miniconda3\python.exe" set "PY=%USERPROFILE%\miniconda3\python.exe"
if not defined PY (
  py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python was not found. Install it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)
echo Starting Chess Insights Builder... your browser will open in a moment.
echo Keep this window open while a report is building. Close it to stop.
if "%PY:~0,2%"=="py" (%PY% insights_builder.py) else ("%PY%" insights_builder.py)
pause

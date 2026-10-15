@echo off
chcp 65001 >nul
set "PYTHON_EXE=D:\mamba\python.exe"
set "APP_DIR=%~dp0"

if not exist "%PYTHON_EXE%" (
  echo [错误] 找不到 %PYTHON_EXE%
  pause
  exit /b 1
)

"%PYTHON_EXE%" "%APP_DIR%ch1_ch2_time_monitor.py"
if errorlevel 1 pause

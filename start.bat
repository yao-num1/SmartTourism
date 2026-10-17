@echo off
setlocal enabledelayedexpansion

:: === PROJECT ROOT — always relative to this script's location ===
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

:: === FIND PYTHON ===
set "PY="

:: 1. venv in project
if exist "%ROOT%\venv\Scripts\python.exe" (
    set "PY=%ROOT%\venv\Scripts\python.exe"
    goto :found
)

:: 2. python on PATH (skip Windows Store stubs)
for %%X in (python3.exe python.exe) do (
    where %%X >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%P in ('where %%X') do (
            echo %%P | findstr /I "WindowsApps" >nul
            if errorlevel 1 (
                set "PY=%%P"
                goto :found
            )
        )
    )
)

:: 3. common install locations
for %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%PROGRAMFILES%\Python311\python.exe"
    "%PROGRAMFILES%\Python310\python.exe"
    "%PROGRAMFILES%\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "C:\Python312\python.exe"
) do (
    if exist %%D (
        set "PY=%%D"
        goto :found
    )
)

echo [ERROR] Python 3.10+ not found.
echo Install from https://python.org or run: python -m venv venv
pause
exit /b 1

:found

:: === ENSURE DATA DIRS ===
if not exist "%ROOT%\data" mkdir "%ROOT%\data"
if not exist "%ROOT%\data\exports" mkdir "%ROOT%\data\exports"

echo ============================================
echo   SmartTourism Server - ZhiLv LingXi
echo ============================================
echo   Root:    %ROOT%
echo   Python:  %PY%
echo   URL:     http://localhost:5050
echo ============================================

:: === INSTALL DEPS (if needed) ===
"%PY%" -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    "%PY%" -m pip install flask flask-cors requests faster-whisper waitress -q
)
"%PY%" -c "import waitress" >nul 2>&1
if errorlevel 1 (
    echo Installing waitress WSGI server...
    "%PY%" -m pip install waitress -q
)

:: === ENSURE OLLAMA RUNNING (for AI shop goods) ===
where ollama >nul 2>&1
if not errorlevel 1 (
    start "Ollama" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
    timeout /t 3 /nobreak >nul
)

:: === START SERVER ===
echo Starting server (waitress WSGI)...
:: 用绝对路径 + /D 指定工作目录：中文路径下 start 派生新窗口时相对路径会失效
start "SmartTourism Server" /D "%ROOT%" "%PY%" "%ROOT%\server\run_server.py"

:: === WAIT & OPEN BROWSER ===
echo Waiting for server...
:waitloop
"%PY%" -c "import urllib.request; urllib.request.urlopen('http://localhost:5050/api/health',timeout=2)" >nul 2>&1
if errorlevel 1 (
    ping 127.0.0.1 -n 2 >nul
    goto waitloop
)
echo Ready! Opening browser...
start http://localhost:5050

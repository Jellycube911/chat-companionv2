@echo off
title Minecraft Companion Brain
cd /d "%~dp0"

echo ==========================================
echo       Minecraft Companion Brain
echo ==========================================
echo.

REM ----- LOCAL AI -----
set "COMPANION_MODEL_PROVIDER=local"
set "COMPANION_LOCAL_BASE_URL=http://127.0.0.1:11434/v1"
set "COMPANION_LOCAL_MODEL=qwen3:8b"

REM ----- OPENAI TEACHER -----
REM Normal thinking stays LOCAL.
REM OpenAI is only used occasionally for learning.
set "COMPANION_CLOUD_TEACHER=on"
set "COMPANION_TEACHER_MODEL=gpt-6-luna"

REM ----- CHECK PYTHON ENVIRONMENT -----
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ERROR: Python virtual environment was not found.
    echo Expected:
    echo %CD%\.venv\Scripts\python.exe
    echo.
    pause
    exit /b 1
)

REM ----- INSTALL / UPDATE REQUIREMENTS -----
if exist "requirements.txt" (
    echo Checking Python requirements...
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt

    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install Python requirements.
        echo.
        pause
        exit /b 1
    )

    echo Python requirements: OK
    echo.
) else (
    echo WARNING: requirements.txt was not found.
    echo Continuing with currently installed packages.
    echo.
)

REM ----- CHECK OLLAMA -----
echo Checking Ollama...

curl -s --fail http://127.0.0.1:11434/v1/models >nul 2>&1

if errorlevel 1 (
    echo.
    echo ERROR: Ollama is not running on port 11434.
    echo Start Ollama first and try again.
    echo.
    pause
    exit /b 1
)

echo Ollama: OK
echo Local model: %COMPANION_LOCAL_MODEL%
echo.

REM ----- CHECK LOCAL MODEL -----
ollama list | findstr /I /C:"%COMPANION_LOCAL_MODEL%" >nul 2>&1

if errorlevel 1 (
    echo WARNING: %COMPANION_LOCAL_MODEL% was not found in Ollama.
    echo.
    echo Installing %COMPANION_LOCAL_MODEL%...
    ollama pull %COMPANION_LOCAL_MODEL%

    if errorlevel 1 (
        echo.
        echo ERROR: Could not download %COMPANION_LOCAL_MODEL%.
        pause
        exit /b 1
    )
)

echo Local model: OK
echo.

REM ----- CHECK OPENAI TEACHER -----
if "%OPENAI_API_KEY%"=="" (
    echo WARNING: OPENAI_API_KEY is not set.
    echo The LOCAL brain will still work.
    echo The OpenAI teacher will NOT work.
) else (
    echo OpenAI teacher: enabled
)

echo.
echo ==========================================
echo Starting Chat...
echo ==========================================
echo.
echo Chat accepts goals like: make a wooden axe
echo.

".venv\Scripts\python.exe" minecraft_agent.py

echo.
echo ==========================================
echo Chat stopped.
echo ==========================================
pause

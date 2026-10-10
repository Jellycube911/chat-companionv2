@echo off
setlocal
title Chat Minecraft Training Lab
cd /d "%~dp0"

REM Training only uses Ollama and the real Minecraft bridge.
set "COMPANION_MODEL_PROVIDER=local"
set "COMPANION_LOCAL_BASE_URL=http://127.0.0.1:11434/v1"
if not defined COMPANION_LOCAL_MODEL set "COMPANION_LOCAL_MODEL=qwen3:8b"
set "COMPANION_CLOUD_TEACHER=off"
set "COMPANION_TRAINING_MODE=on"

echo ==========================================
echo       Chat Minecraft Training Lab
echo ==========================================
echo.
echo Keep Minecraft and Ollama open.
echo CLOSE the normal Chat brain first.
echo Practice physically changes natural terrain.
echo No item spawning or teleporting. Memory is kept.
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" training_lab.py %*
) else (
    py -3 training_lab.py %*
)

if errorlevel 1 (
    echo.
    echo Training did not complete. Check the error above.
    pause
)
endlocal

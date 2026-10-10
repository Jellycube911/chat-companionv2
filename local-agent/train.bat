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

REM Each cycle runs the full curriculum (observe, orient, navigate, mine, collect).
REM The trainer allows up to 100 cycles; use 120 minutes for long sessions.
REM Existing no-progress and pending-job safety stops still apply.
echo.
set "TRAIN_ROUNDS="
set /p "TRAIN_ROUNDS=How many training cycles? (1-100, Enter for 30): "
if not defined TRAIN_ROUNDS set "TRAIN_ROUNDS=30"

echo.
echo Starting %TRAIN_ROUNDS% cycles (maximum session time: 120 minutes).
echo You can stop at any time with Ctrl+C.
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" training_lab.py --rounds "%TRAIN_ROUNDS%" --minutes 120 %*
) else (
    py -3 training_lab.py --rounds "%TRAIN_ROUNDS%" --minutes 120 %*
)

if errorlevel 1 (
    echo.
    echo Training did not complete. Check the error above.
    pause
)
endlocal

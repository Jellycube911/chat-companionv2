@echo off
setlocal
title Chat Memory Learning Test
cd /d "%~dp0"

REM This is an independent physical evaluation, not training/graduation.
REM Existing SQLite memories and previous reports are NEVER deleted.
set "COMPANION_MODEL_PROVIDER=local"
set "COMPANION_LOCAL_BASE_URL=http://127.0.0.1:11434/v1"
if not defined COMPANION_LOCAL_MODEL set "COMPANION_LOCAL_MODEL=qwen3:8b"
set "COMPANION_CLOUD_TEACHER=off"
set "COMPANION_TRAINING_MODE=on"

echo =====================================
echo    Minecraft Learning Memory Test
echo =====================================
echo Keep Minecraft and Ollama running.
echo Stop run.bat and train.bat before this evaluation.
echo Chat will PHYSICALLY explore, mine, and collect.
echo Each test changes natural terrain. No world reset or cheats.
echo Compares Qwen with prior learned experience versus without.
echo Results are indicative, not conclusive proof of learning.
echo.
echo 6 trials total: 3 memory-on, 3 memory-off, randomized order.
echo Reports are checkpointed under data\training_reports.
echo.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" memory_ablation.py --trials 6 --minutes 60 %*
) else (
    py -3 memory_ablation.py --trials 6 --minutes 60 %*
)
if errorlevel 1 (
    echo.
    echo Test stopped or failed. Partial report is preserved.
    pause
)
endlocal

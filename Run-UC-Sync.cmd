@echo off
title Universal Credit Work-Search Sync
cd /d "%~dp0"
if "%~1"=="" (
    python uc_job_sync.py
) else (
    python uc_job_sync.py --file "%~1"
)
pause

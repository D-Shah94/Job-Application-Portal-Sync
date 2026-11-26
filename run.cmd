@echo off
title UC Work Search Sync
cd /d "%~dp0"
if "%~1"=="" (
    python -m worksearch_sync
) else (
    python -m worksearch_sync --file "%~1"
)
pause

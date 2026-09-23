@echo off
rem ---------------------------------------------------------------------------
rem  Derivative Monthly Pricing - double-click launcher.
rem
rem  Written for someone who does not use a terminal: every failure below
rem  explains itself in plain language and tells the user what to pass on.
rem  The one-time prerequisite is "uv" - see SETUP.md.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

title Derivative Monthly Pricing
echo.
echo   Derivative Monthly Pricing
echo   ==========================
echo.

rem --- Prerequisite check -----------------------------------------------------
where uv >nul 2>nul
if errorlevel 1 goto :no_uv

rem --- Prepare components. First run downloads them and takes a few minutes. --
echo   Preparing the tool. The first run takes a few minutes; later runs are quick.
echo.
call uv sync --group ui --quiet
if errorlevel 1 goto :sync_failed

rem --- Launch -----------------------------------------------------------------
echo.
echo   Starting up. Your web browser will open by itself in a moment.
echo.
echo   IMPORTANT: leave this black window open while you work.
echo   Closing it shuts the tool down. That is also how you exit when done.
echo.
call uv run streamlit run ui/app.py
echo.
echo   The tool has stopped.
pause
exit /b 0

rem --- Failure paths ----------------------------------------------------------
:no_uv
echo   [ One-time setup is needed ]
echo.
echo   This tool needs a helper program called "uv", which is not installed
echo   on this computer yet. You cannot fix this by double-clicking anything;
echo   someone needs to install it once.
echo.
echo   Please copy the line below and send it to whoever gave you this tool:
echo.
echo       "start-app.cmd says uv is not installed - please run the one-time setup"
echo.
pause
exit /b 1

:sync_failed
echo.
echo   [ Could not finish preparing the tool ]
echo.
echo   This almost always means the computer could not download what it needs.
echo   Common causes: no internet connection, not signed in to the company
echo   VPN, or the company network blocking the download.
echo.
echo   Try connecting to the VPN and double-clicking this file again. If it
echo   still fails, send the message below to whoever gave you this tool:
echo.
echo       "start-app.cmd failed at the uv sync step"
echo.
pause
exit /b 1

@echo off
rem ---------------------------------------------------------------------------
rem  Derivative Monthly Pricing - double-click launcher.
rem
rem  Written for someone who does not use a terminal: every failure below
rem  explains itself in plain language and tells the user what to pass on.
rem  Each failure is ALSO written to SETUP-PROBLEM.txt next to this file, so the
rem  message survives the window being closed at the "press any key" prompt.
rem  The one-time prerequisite is "uv" - see SETUP.md.
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

title Derivative Monthly Pricing
set "PROBLEM=%~dp0SETUP-PROBLEM.txt"
if exist "%PROBLEM%" del "%PROBLEM%" >nul 2>nul

echo.
echo   Derivative Monthly Pricing
echo   ==========================
echo.

rem --- Folder-path length check ----------------------------------------------
rem  Windows refuses to open files past 260 characters unless long-path support
rem  is switched on, which it is not by default. The dependencies we install
rem  nest up to 140 characters below this folder, so this folder's own path has
rem  to stay under 120. Over that, Python fails with an inscrutable
rem  ModuleNotFoundError partway through starting up - so we catch it here.
set "HERE=%~dp0"
if "!HERE:~-1!"=="\" set "HERE=!HERE:~0,-1!"
if not "!HERE:~120!"=="" goto :path_too_long

rem --- Locate uv --------------------------------------------------------------
rem  PATH first. If that misses, check the places the installers actually put
rem  it: a fresh "uv" install updates PATH only for NEW processes, so a
rem  double-clicked shortcut inherits Explorer's stale PATH and cannot see it.
rem  Finding the .exe directly means the user does not have to reboot.
set "UV="
where uv >nul 2>nul
if not errorlevel 1 set "UV=uv"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" set "UV=%LOCALAPPDATA%\Programs\uv\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
if not defined UV if exist "%USERPROFILE%\scoop\shims\uv.exe" set "UV=%USERPROFILE%\scoop\shims\uv.exe"
if not defined UV if exist "%USERPROFILE%\AppData\Roaming\uv\uv.exe" set "UV=%USERPROFILE%\AppData\Roaming\uv\uv.exe"
if not defined UV if exist "%ProgramFiles%\uv\uv.exe" set "UV=%ProgramFiles%\uv\uv.exe"
if not defined UV goto :no_uv

rem --- Prepare components. First run downloads them and takes a few minutes. --
echo   Preparing the tool. The first run takes a few minutes; later runs are quick.
echo   Leave this window alone until your browser opens.
echo.
call "%UV%" sync --group ui
if errorlevel 1 goto :sync_failed

rem --- Launch -----------------------------------------------------------------
echo.
echo   Starting up. Your web browser will open by itself in a moment.
echo   If it does not, open your browser and go to:  http://localhost:8501
echo.
echo   IMPORTANT: leave this black window open while you work.
echo   Closing it shuts the tool down. That is also how you exit when done.
echo.
call "%UV%" run streamlit run ui/app.py
if errorlevel 1 goto :run_failed
echo.
echo   The tool has stopped.
pause
exit /b 0

rem --- Failure paths ----------------------------------------------------------
:path_too_long
echo   [ This folder is buried too deep on the disk ]
echo.
echo   Windows cannot handle the very long file names this tool needs when the
echo   folder itself sits this deep. Nothing is broken - it just has to live
echo   closer to the top of the drive.
echo.
echo   This folder is:
echo       !HERE!
echo.
echo   Please MOVE the whole folder somewhere shorter and run this file again.
echo   A good choice is directly under your C: drive, for example:
echo       C:\pricing-tool
echo.
echo   Avoid putting it inside OneDrive or Teams folders - those paths are
echo   usually far too long on their own.
echo.
>>"%PROBLEM%" echo [ This folder is buried too deep on the disk ]
>>"%PROBLEM%" echo Folder: !HERE!
>>"%PROBLEM%" echo Move the folder to something short such as C:\pricing-tool
call :saved
exit /b 1

:no_uv
echo   [ One-time setup is needed ]
echo.
echo   This tool needs a helper program called "uv". It is either not installed
echo   on this computer, or it was installed so recently that Windows has not
echo   picked it up yet.
echo.
echo   TRY THIS FIRST: restart the computer, then double-click this file again.
echo   A restart is what makes a brand-new "uv" install visible to Windows.
echo.
echo   If it still says this after a restart, uv is genuinely missing. Copy the
echo   line below and send it to whoever gave you this tool:
echo.
echo       "start-app.cmd says uv is not installed - please run the one-time setup"
echo.
>>"%PROBLEM%" echo [ One-time setup is needed ]
>>"%PROBLEM%" echo uv was not found on PATH or in any standard install location.
>>"%PROBLEM%" echo Checked: PATH, %%USERPROFILE%%\.local\bin, %%LOCALAPPDATA%%\Programs\uv,
>>"%PROBLEM%" echo WinGet Links, scoop shims, %%APPDATA%%\uv, %%ProgramFiles%%\uv
>>"%PROBLEM%" echo Fix: restart the computer, then retry. If unchanged, reinstall uv.
call :saved
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
echo   still fails, send the message below - and a photo of this window - to
echo   whoever gave you this tool:
echo.
echo       "start-app.cmd failed at the uv sync step"
echo.
>>"%PROBLEM%" echo [ Could not finish preparing the tool ]
>>"%PROBLEM%" echo The "uv sync --group ui" step failed. Usually no network / no VPN,
>>"%PROBLEM%" echo or the company proxy blocking downloads from pypi.org.
>>"%PROBLEM%" echo uv used: %UV%
call :saved
exit /b 1

:run_failed
echo.
echo   [ The tool could not start ]
echo.
echo   Everything downloaded correctly, but the tool itself failed to launch.
echo   Please send a photo of this window to whoever gave you this tool.
echo.
>>"%PROBLEM%" echo [ The tool could not start ]
>>"%PROBLEM%" echo Dependencies installed, but "streamlit run ui/app.py" exited with an error.
>>"%PROBLEM%" echo uv used: %UV%
call :saved
exit /b 1

:saved
echo   ---------------------------------------------------------------------
echo   This message has also been saved to the file SETUP-PROBLEM.txt, in
echo   this same folder. You can close this window and send that file on.
echo   ---------------------------------------------------------------------
echo.
pause
goto :eof

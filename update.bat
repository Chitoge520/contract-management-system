@echo off
rem Download the latest code from GitHub. Data (contracts, users, settings) in the data folder is not touched.
if /i "%~1"=="__run" goto main
start "Update Contract Register" cmd /k call "%~f0" __run
exit /b

:main
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 goto nogit
if not exist ".git" goto norepo
git pull --ff-only origin main
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt 2>nul
echo.
echo Updated. Close the "Contract Register" window and run start_lan.bat (or start.bat) again.
goto end

:norepo
echo [ERROR] This folder was not downloaded with git clone, so it cannot update itself.
echo   Download the latest ZIP from GitHub and copy it over this folder (keep the data folder).
goto end

:fail
echo [ERROR] Update failed. Files in this folder may have been edited by hand; tell the developer.
goto end

:nogit
echo [ERROR] Git is not installed. Install from https://git-scm.com/download/win

:end
echo.
echo You can close this window.

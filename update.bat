@echo off
rem Download the latest code from GitHub. Data (contracts, users, settings) in the data folder is not touched.
if /i "%~1"=="__run" goto main
start "Update Contract Register" cmd /k call "%~f0" __run
exit /b

:main
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 goto nogit
if not exist ".git" goto linkrepo
git pull --ff-only origin main
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt 2>nul
echo.
echo Updated. Close the "Contract Register" window and run start_lan.bat (or start.bat) again.
goto end

:linkrepo
rem Folder was copied (e.g. from a USB stick): link it to GitHub once, then take the latest code
echo Linking this folder to GitHub (first time only)...
git init
git remote add origin https://github.com/Chitoge520/contract-management-system.git
git fetch origin main
if errorlevel 1 goto fail
git reset --hard FETCH_HEAD
git branch -M main
git branch --set-upstream-to=origin/main main
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt 2>nul
echo.
echo Updated. Close the "Contract Register" window and run start_lan.bat (or start.bat) again.
goto end

:fail
echo [ERROR] Update failed. If a GitHub login window appeared, sign in and run update.bat again.
goto end

:nogit
echo [ERROR] Git is not installed. Install from https://git-scm.com/download/win

:end
echo.
echo You can close this window.

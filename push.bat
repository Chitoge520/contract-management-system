@echo off
rem Upload code changes to GitHub (contracts, data and API key are excluded by .gitignore)
if /i "%~1"=="__run" goto main
start "Push to GitHub" cmd /k call "%~f0" __run
exit /b

:main
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 goto nogit

if exist ".git" goto haverepo
git init
git remote add origin https://github.com/Chitoge520/contract-management-system.git
rem Link this folder to the code already on GitHub (keeps your local files as they are)
git fetch origin
git reset origin/main
git branch -M main

:haverepo
git config user.name >nul 2>nul
if errorlevel 1 git config user.name "Chitoge520"
git config user.email >nul 2>nul
if errorlevel 1 git config user.email "Chitoge520@users.noreply.github.com"

git add -A
git commit -m "Update %date% %time%"
git pull --rebase origin main 2>nul
git push -u origin main
if errorlevel 1 goto pushfail
echo.
echo Done: code is on https://github.com/Chitoge520/contract-management-system
goto end

:pushfail
echo.
echo [ERROR] Push failed. If a GitHub login window appeared, sign in and run push.bat again.
goto end

:nogit
echo [ERROR] Git is not installed or not on PATH. Install from https://git-scm.com/download/win

:end
echo.
echo You can close this window.

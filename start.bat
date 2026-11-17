@echo off
rem Re-open in a window that stays open, so errors are always visible
if /i "%~1"=="__run" goto main
start "Contract Register" cmd /k call "%~f0" __run %1
exit /b

:main
setlocal
cd /d "%~dp0"
set "LOG=%~dp0start.log"
echo ===== %date% %time% =====> "%LOG%"
echo Folder: %cd%>> "%LOG%"

rem ---- find Python ----
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto checkpy
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto checkpy
goto nopython

:checkpy
echo Python command: %PY%
echo Python command: %PY%>> "%LOG%"
%PY% --version>> "%LOG%" 2>&1
%PY% -c "import sys; print(sys.version)"
if errorlevel 1 goto nopython
%PY% -c "import sys; sys.exit(0 if (3,9) <= sys.version_info[:2] <= (3,14) else 1)"
if errorlevel 1 goto badversion

if exist ".venv\Scripts\python.exe" goto run

rem ---- first run: create local environment ----
echo.
echo First run: installing components. This takes 3-5 minutes, please wait...
echo.
echo Creating .venv>> "%LOG%"
%PY% -m venv .venv>> "%LOG%" 2>&1
if errorlevel 1 goto venvfail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt --log "%~dp0pip.log"
if errorlevel 1 goto pipfail
echo Install OK>> "%LOG%"

:run
echo Starting server>> "%LOG%"
".venv\Scripts\python.exe" run.py %2
echo Server stopped, exit code %errorlevel%>> "%LOG%"
goto end

:nopython
echo [ERROR] Python not found.>> "%LOG%"
echo.
echo [ERROR] Python was not found on this computer.
echo   Install Python 3.12 from https://www.python.org/downloads/
echo   During installation tick "Add python.exe to PATH", then run start.bat again.
goto end

:badversion
echo [ERROR] Unsupported Python version.>> "%LOG%"
echo.
echo [ERROR] This program needs Python 3.9 - 3.14. Please install Python 3.12.
goto end

:venvfail
echo [ERROR] venv creation failed.>> "%LOG%"
echo.
echo [ERROR] Could not create the .venv folder. See start.log
goto end

:pipfail
echo [ERROR] pip install failed.>> "%LOG%"
echo.
echo [ERROR] Installing components failed. Check the internet / proxy, then run start.bat again.
echo   Details are in pip.log
rmdir /s /q .venv
goto end

:end
echo.
echo You can close this window.

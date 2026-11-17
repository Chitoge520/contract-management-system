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

rem ---- find Python: prefer a supported version even if a newer one is the default ----
set "PY="
rem never let the Python install manager download runtimes silently while probing
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYTHON_MANAGER_CONFIRM=false"
where py >nul 2>nul
if errorlevel 1 goto trypath
for %%V in (3.14 3.13 3.12 3.11 3.10) do if not defined PY call :trypy %%V
if defined PY goto checkpy
set "PY=py -3"
goto checkpy

:trypath
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
%PY% -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)"
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
echo [ERROR] This program needs Python 3.10 - 3.14 (Python 3.15 is too new for some components).
echo   Please also install Python 3.13 from https://www.python.org/downloads/
echo   You do NOT need to uninstall your current Python. Then run start.bat again.
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
goto :eof

:trypy
py -%1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -%1"
exit /b

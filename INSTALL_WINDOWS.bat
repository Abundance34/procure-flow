@echo off
setlocal
cd /d "%~dp0"
echo Installing ProcureFlow in a short local virtual environment named .v ...
echo.
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -m venv .v
) else (
    python -m venv .v
)
if not exist ".v\Scripts\activate.bat" (
    echo Failed to create virtual environment. Make sure Python is installed and added to PATH.
    pause
    exit /b 1
)
call .v\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo Install failed. If you still see a Windows Long Path error, move this folder to C:\pf and run INSTALL_WINDOWS.bat again.
    pause
    exit /b 1
)
echo.
echo Installation complete.
echo Run RUN_APP.bat to start ProcureFlow.
pause

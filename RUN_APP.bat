@echo off
setlocal
cd /d "%~dp0"
if not exist ".v\Scripts\activate.bat" (
    echo Virtual environment not found. Run INSTALL_WINDOWS.bat first.
    pause
    exit /b 1
)
call .v\Scripts\activate.bat
streamlit run app.py
pause

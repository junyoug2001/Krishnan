@echo off
setlocal

rem Run from the folder containing Krishnan and andor-spectroscopy.
pushd "%~dp0.."
if errorlevel 1 (
    echo Could not open the Krishnan project folder.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Krishnan is not installed in this folder yet.
    echo Follow the Windows setup steps in Krishnan\README.md first.
    pause
    popd
    exit /b 1
)

rem pythonw keeps the GUI and its hardware backends free of console windows.
start "" ".venv\Scripts\pythonw.exe" -m krishnan
popd

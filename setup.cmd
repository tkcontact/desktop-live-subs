@echo off
rem Works from a \\wsl.localhost path: pushd maps it to a temporary drive letter.
pushd "%~dp0"
set "VENV=%LOCALAPPDATA%\desktop-live-subs\venv"
if not exist "%VENV%\Scripts\python.exe" py -3.13 -m venv "%VENV%"
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
popd

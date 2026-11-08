@echo off
rem Console version for debugging (logs are printed here as well as logs\app.log).
rem Works from a \\wsl.localhost path: pushd maps it to a temporary drive letter.
pushd "%~dp0"
"%LOCALAPPDATA%\desktop-live-subs\venv\Scripts\python.exe" main.py %*
popd

@echo off
rem Works from a \\wsl.localhost path: pushd maps it to a temporary drive letter.
pushd "%~dp0"
set "VENV=%LOCALAPPDATA%\desktop-live-subs\venv"
if not exist "%VENV%\Scripts\python.exe" py -3.13 -m venv "%VENV%"
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
rem 読み上げ用 (CUDA 版の PyTorch を先に入れる)
"%VENV%\Scripts\python.exe" -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
"%VENV%\Scripts\python.exe" -m pip install -r requirements-tts.txt
popd

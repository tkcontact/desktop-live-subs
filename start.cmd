@echo off
chcp 65001 >nul
setlocal
rem ダブルクリックで起動: Strata を (Whisper と読み上げの分の VRAM を空けて) 起動してから、字幕ツールを起動する
set "HERE=%~dp0"
set "VENV=%LOCALAPPDATA%\desktop-live-subs\venv"
set "STRATA=%LOCALAPPDATA%\Strata\terminal\current\bin\strata.cmd"
rem \\wsl.localhost のフォルダは作業フォルダにできないので、ローカルのフォルダに移ってから動かす
cd /d "%LOCALAPPDATA%\desktop-live-subs"

echo Whisper と読み上げの VRAM 使用量を確認しています (初回は数分かかります)...
rem cmd /c は先頭と末尾の引用符を剥がすので、コマンド全体をもう一重の引用符で囲む
for /f "usebackq delims=" %%r in (`""%VENV%\Scripts\python.exe" "%HERE%vram.py" --strata-args"`) do set "STRATA_ARGS=%%r"
if not defined STRATA_ARGS (
  echo VRAM の使用量を測れませんでした。setup.cmd を実行したか確認してください。
  pause
  exit /b 1
)

echo Strata を起動しています (%STRATA_ARGS%)...
call "%STRATA%" launch --preset live-subs %STRATA_ARGS% --restart --yes --no-tui
if errorlevel 1 (
  echo Strata を起動できませんでした。字幕ツールは起動して、Strata の復旧を待ちます。
  timeout /t 5 >nul
)

start "" "%VENV%\Scripts\pythonw.exe" "%HERE%main.py"

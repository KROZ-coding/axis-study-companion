@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHON=%ROOT%server\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%ROOT%..\..\server\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Python environment not found. Create demo\recording-demo\server\.venv and install server\requirements.txt.
  pause
  exit /b 1
)
pushd "%ROOT%"
echo AXIS demo: http://127.0.0.1:8010/prototype/
echo Keep this window open. Press Ctrl+C to stop.
"%PYTHON%" -I -B -X utf8 "%ROOT%server\run_recording_demo.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%

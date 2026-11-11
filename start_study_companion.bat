@echo off
setlocal
set "ROOT=%~dp0"
set "SERVER=%ROOT%server"
set "VENV=%SERVER%\.venv"
set "PORT=%STUDY_COMPANION_PORT%"
if "%PORT%"=="" set "PORT=8000"

if not exist "%VENV%\Scripts\python.exe" (
  echo Python environment not found. Run the setup commands in README.md first.
  exit /b 1
)

pushd "%SERVER%"
"%VENV%\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port "%PORT%"
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

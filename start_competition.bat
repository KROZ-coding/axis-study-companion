@echo off
setlocal
set "SERVER=%~dp0server"
if not exist "%SERVER%\.venv\Scripts\python.exe" (
  echo Python environment not found. Follow README.md to install dependencies first.
  exit /b 1
)
pushd "%SERVER%"
"%SERVER%\.venv\Scripts\python.exe" -m app.competition_launcher %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set "PYTHON=py -3"
) else (
  set "PYTHON=python"
)

echo Starting chatbot-win sidebar frontend...
echo.
echo Default: app window mode + local WeFlow auto-start.
echo Optional: start_sidebar.cmd --mode server --port 8765
echo Optional: start_sidebar.cmd --weflow off
echo Optional: start_sidebar.cmd --weflow on --install-weflow-deps always
echo Optional: start_sidebar.cmd --weflow-window normal
echo Logs: data\weflow_process.out.log and data\weflow_process.err.log
echo.

%PYTHON% scripts\start_sidebar_frontend.py %*
set "EXIT_CODE=%errorlevel%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Sidebar launcher exited with code %EXIT_CODE%.
  pause
)

exit /b %EXIT_CODE%

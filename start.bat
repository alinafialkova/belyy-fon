@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Белый фон
set PYTHONUNBUFFERED=1

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 goto havepy
python -c "import sys" >nul 2>&1
if not errorlevel 1 goto havepython

echo Поставь Python с python.org и запусти этот файл ещё раз.
pause
exit /b 1

:havepy
set "PY=py -3"
goto run

:havepython
set "PY=python"

:run
%PY% -c "import rembg, PIL, onnxruntime" >nul 2>&1
if not errorlevel 1 goto app
echo Ставлю нужные библиотеки, подожди
%PY% -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto fail
%PY% -m pip install --force-reinstall "onnxruntime-directml==1.24.4"
if errorlevel 1 goto fail

:app
%PY% app.py
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
pause
exit /b 1

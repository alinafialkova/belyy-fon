@echo off
cd /d "%~dp0"
title Белый фон
set PYTHONUNBUFFERED=1
echo Запуск...

set "PY="
py -3 -c "import sys; assert 'WindowsApps' not in sys.executable" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys; assert 'WindowsApps' not in sys.executable" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python не найден. Поставь его с python.org и запусти этот файл ещё раз.
  echo.
  pause
  exit /b 1
)

%PY% -c "import rembg, PIL, onnxruntime" >nul 2>&1
if not errorlevel 1 goto app

echo Ставлю библиотеки. Это несколько минут, окно не закрывай.
%PY% -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto fail
%PY% -m pip install --force-reinstall "onnxruntime-directml==1.24.4"
if errorlevel 1 goto fail

:app
echo Открываю. Это окно не закрывай, пока пользуешься.
%PY% app.py
echo.
echo Программа остановилась.
pause
exit /b 0

:fail
echo.
echo Не получилось поставить библиотеки.
pause
exit /b 1

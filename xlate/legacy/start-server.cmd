@echo off
rem ============================================================
rem  xlate 本地词库服务 —— 双击本文件即可启动
rem
rem  作用：给 Tampermonkey 里的 xlate 用户脚本提供 glossary.json
rem  地址：http://127.0.0.1:8765/glossary.json
rem  停止：关掉这个窗口，或按 Ctrl+C
rem
rem  为什么需要它：
rem    Chrome MV3 之后，浏览器扩展基本无法用 XHR 读取 file:// 本地文件，
rem    所以只能起一个本地 HTTP 服务。
rem ============================================================

chcp 65001 >nul 2>&1
title xlate 本地词库服务

set "PY=C:\Users\14386\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
set "DIR=%~dp0"
set "PORT=8765"

if not exist "%PY%" (
  echo.
  echo [错误] 找不到内置 Python：
  echo   %PY%
  echo.
  echo 如果你改过 DSH 的运行时位置，请修改本文件里的 PY 变量。
  echo.
  pause
  exit /b 1
)

if not exist "%DIR%glossary.json" (
  echo.
  echo [错误] 同目录下找不到 glossary.json：
  echo   %DIR%glossary.json
  echo.
  pause
  exit /b 1
)

rem 先看端口是不是已经被占用（可能是上一次没关干净）
netstat -ano | findstr /r /c:"LISTENING" | findstr /c:":%PORT% " >nul 2>&1
if not errorlevel 1 (
  echo.
  echo [提示] 端口 %PORT% 已被占用，可能服务已经在跑了。
  echo        先试试浏览器能不能打开下面这个地址：
  echo        http://127.0.0.1:%PORT%/glossary.json
  echo.
  echo        如果已经能打开，就不需要再启动，直接关掉本窗口。
  echo.
  pause
)

echo.
"%PY%" "%DIR%serve.py" --port %PORT% --dir "%DIR%"

echo.
echo 服务已退出。
pause

@echo off
setlocal
cd /d "%~dp0"

set "PY="

rem ---- 1) 优先使用项目内自带的 .venv（存在但缺依赖时自动补装） ----
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if defined PY goto :venv_check

rem ---- 2) 在系统里找一个「已装好依赖」的 Python ----
call :probe py
if defined PY goto :launch
call :probe python
if defined PY goto :launch

rem ---- 3) 都没装依赖：自动创建 .venv 并安装（仅首次需要联网） ----
echo [首次运行] 正在创建运行环境并安装依赖，需要联网，请耐心等待...
py -3 -m venv "%~dp0.venv" 2>nul
if not exist "%~dp0.venv\Scripts\python.exe" python -m venv "%~dp0.venv" 2>nul
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto :no_python

"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :deps_fail
goto :launch

:probe
if defined PY exit /b 0
for /f "delims=" %%P in ('%1 -c "import sys;print(sys.executable)" 2^>nul') do (
  "%%P" -c "import webview,requests,jwcrypto,cryptography" >nul 2>nul && set "PY=%%P"
)
exit /b 0

:venv_check
rem .venv 存在但依赖可能不完整（首次安装被中断 / venv 被拷坏）：先自检，缺失则补装，
rem 而不是直接跳到 :deps_fail 让用户手动修——那样非技术用户无从恢复。
"%PY%" -c "import webview,requests,jwcrypto,cryptography" >nul 2>nul
if not errorlevel 1 goto :launch
echo [提示] 已找到 .venv，但运行依赖不完整，正在补装依赖（需要联网，请耐心等待）...
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :deps_fail
goto :launch

:launch
rem 启动前再自检一次：依赖缺失时留在控制台说明原因，而不是无声闪退。
rem cryptography 必须一起检查——它缺失时程序仍能跑起来，但凭据会静默降级为明文落盘。
"%PY%" -c "import webview,requests,jwcrypto,cryptography" >nul 2>nul
if errorlevel 1 goto :deps_fail

set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" dashboard.py
exit /b 0

:no_python
echo.
echo [错误] 未找到 Python 3，无法创建运行环境。
echo 请先安装 Python 3.8 或更高版本（安装时务必勾选 "Add python.exe to PATH"），再重新运行本脚本。
echo 下载地址：https://www.python.org/downloads/
pause
exit /b 1

:deps_fail
echo.
echo [错误] 运行依赖不完整，程序无法启动（此前的"闪退"就是这里引起的）。
echo 请手动执行以下命令安装依赖后重试：
echo     "%PY%" -m pip install -r requirements.txt
pause
exit /b 1

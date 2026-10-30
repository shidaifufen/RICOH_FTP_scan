@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo  扫描直连 · 一键注册  依赖安装
echo ============================================================
echo.

set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if not defined PY call :which python.exe
if not defined PY goto :nopython

echo  使用 Python: %PY%
"%PY%" -c "import sys; print('  版本:', sys.version)"
echo.
echo  正在把依赖装进 vendor 目录……
"%PY%" -m pip install --upgrade --target "%~dp0vendor" -r "%~dp0requirements.txt" --no-warn-script-location --disable-pip-version-check
if errorlevel 1 goto :failed

echo.
echo  [OK] 依赖已就绪，现在可以双击 启动.bat 或 扫描直连.exe
echo.
pause
exit /b 0

:nopython
echo  [x] 没有找到 Python。
echo      请先安装 Python 3.9 及以上版本（安装时勾选 Add python.exe to PATH）：
echo      https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:failed
echo.
echo  [x] 安装失败。可尝试国内镜像：
echo      "%PY%" -m pip install --target "%~dp0vendor" -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:which
for /f "delims=" %%I in ('where %~1 2^>nul') do (
    if not defined PY (
        echo %%I | findstr /i "WindowsApps" >nul || set "PY=%%I"
    )
)
goto :eof
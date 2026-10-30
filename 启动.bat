@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  扫描直连 · 一键注册  启动脚本
rem  优先启动「文件夹版」（启动快），没有再退到「单文件版」。
rem  注意：本文件用 GBK 保存；控制流全 ASCII，中文只用于提示。
rem ============================================================

if exist "%~dp0文件夹版\扫描直连.exe" (
    start "" "%~dp0文件夹版\扫描直连.exe"
    exit /b 0
)
if exist "%~dp0单文件版\扫描直连.exe" (
    start "" "%~dp0单文件版\扫描直连.exe"
    exit /b 0
)

set "EXE="
for /f "delims=" %%F in ('dir /b /o-s "%~dp0*.exe" 2^>nul') do if not defined EXE set "EXE=%~dp0%%~F"
if defined EXE (
    start "" "%EXE%"
    exit /b 0
)

set "PY="
if exist "%~dp0runtime\pythonw.exe" set "PY=%~dp0runtime\pythonw.exe"
if not defined PY call :which pythonw.exe
if not defined PY call :which python.exe
if not defined PY goto :nopython
"%PY%" "%~dp0main.py"
if errorlevel 1 pause
exit /b 0

:nopython
echo.
echo  [x] 没找到可运行的程序（文件夹版/单文件版都不在）。
echo      请确认本目录下有 文件夹版\ 或 单文件版\ 子目录。
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
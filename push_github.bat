@echo off
setlocal

set "REPO=C:\pyRevit\Extensions\An-Tools\An-Tools.extension"

cd /d "%REPO%" || (
    echo [ERROR] Khong tim thay thu muc repo:
    echo %REPO%
    pause
    exit /b 1
)

echo.
echo ============================================
echo   An-Tools - Auto Push to GitHub
echo ============================================
echo.

git status --short

git add .

git diff --cached --quiet
if %errorlevel%==0 (
    echo.
    echo Khong co thay doi moi de cap nhat.
    pause
    exit /b 0
)

git commit -m "Auto update %date% %time%"
if errorlevel 1 (
    echo.
    echo [ERROR] Commit that bai.
    pause
    exit /b 1
)

git push origin main
if errorlevel 1 (
    echo.
    echo [ERROR] Push len GitHub that bai.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Da cap nhat GitHub thanh cong.
echo ============================================
echo.
pause

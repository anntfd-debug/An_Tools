@echo off
setlocal EnableExtensions

set "REPO=C:\pyRevit\Extensions\An-Tools\An-Tools.extension"

cd /d "%REPO%" || (
    echo [ERROR] Khong tim thay thu muc repo:
    echo %REPO%
    pause
    exit /b 1
)

echo.
echo ============================================
echo   An-Tools - Sync and Push to GitHub
echo ============================================
echo.

git branch -M main

echo [1/4] Dang dong bo thay doi tu GitHub...
git pull --rebase origin main
if errorlevel 1 (
    echo.
    echo [ERROR] Pull/Rebase that bai.
    echo Neu Git bao conflict, can xu ly conflict truoc khi push.
    pause
    exit /b 1
)

echo.
echo [2/4] Dang them thay doi...
git add -A

git diff --cached --quiet
if %errorlevel%==0 (
    echo.
    echo Khong co thay doi moi de commit.
    echo Repo da duoc dong bo voi GitHub.
    pause
    exit /b 0
)

echo.
echo [3/4] Dang tao commit...
git commit -m "Auto update %date% %time%"
if errorlevel 1 (
    echo.
    echo [ERROR] Commit that bai.
    pause
    exit /b 1
)

echo.
echo [4/4] Dang push len GitHub...
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

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

echo [1/4] Dang kiem tra thay doi tren may...
git add -A

git diff --cached --quiet
if errorlevel 1 (
    echo.
    echo [2/4] Dang tao commit...
    git commit -m "Auto update %date% %time%"
    if errorlevel 1 (
        echo.
        echo [ERROR] Commit that bai.
        pause
        exit /b 1
    )
) else (
    echo Khong co file moi can commit.
)

echo.
echo [3/4] Dang dong bo voi GitHub...
git pull --rebase origin main
if errorlevel 1 (
    echo.
    echo ============================================
    echo [ERROR] Pull/Rebase that bai.
    echo Co the Git dang gap conflict.
    echo KHONG tiep tuc push de tranh mat code.
    echo ============================================
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
echo   Da dong bo GitHub thanh cong.
echo ============================================
echo.
pause
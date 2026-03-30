@echo off
cd /d "%~dp0"

python -m PyInstaller --clean --noconfirm --onefile --windowed --name UWONavi UWONavi.py

if errorlevel 1 (
    echo.
    echo PyInstaller build failed.
    pause
    exit /b 1
)

copy /y UWONavi.ini dist\
copy /y nums.bmp dist\
copy /y map.png dist\
copy /y BlueBlackMap.png dist\

echo.
echo Build complete.
pause
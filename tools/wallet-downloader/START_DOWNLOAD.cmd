@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 wallet_download.py --out "D:\Phavi\PolyWalletResearch"
  goto done
)
where python >nul 2>nul
if not errorlevel 1 (
  python wallet_download.py --out "D:\Phavi\PolyWalletResearch"
  goto done
)
echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/windows/ and run this file again.
:done
pause

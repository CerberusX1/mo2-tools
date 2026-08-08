@echo off
python changelog.py %*
pause
:: run_changelog.bat
@echo off
python "%~dp0changelog.py" %*
pause

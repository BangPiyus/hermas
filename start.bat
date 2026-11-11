@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python setup_local.py
uvicorn hermas.web:create_app --factory --host 127.0.0.1 --port 8765 --workers 1

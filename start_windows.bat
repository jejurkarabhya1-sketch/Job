@echo off
if not exist .venv py -3.12 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m uvicorn app.main:app --reload

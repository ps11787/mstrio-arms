@echo off
setlocal
cd /d "%~dp0"

set "MSTR_BASE_URL=%MSTR_BASE_URL%"
set "MSTR_SOURCE_ENVIRONMENT=%MSTR_SOURCE_ENVIRONMENT%"
set "MSTR_USERNAME=%MSTR_USERNAME%"
set "MSTR_PASSWORD=%MSTR_PASSWORD%"
set "MSTR_PROJECT_ID=%MSTR_PROJECT_ID%"
set "MSTR_DATASOURCE_INSTANCE_ID=%MSTR_DATASOURCE_INSTANCE_ID%"
set "MSTR_TEST_URL=%MSTR_TEST_URL%"
set "MSTR_QA_URL=%MSTR_QA_URL%"
set "MSTR_PROD_URL=%MSTR_PROD_URL%"

if not exist ".venv\Scripts\python.exe" (
    echo Python virtual environment not found at .venv\Scripts\python.exe
    exit /b 1
)

call ".venv\Scripts\python.exe" import_package.py
exit /b %ERRORLEVEL%

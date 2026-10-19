$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$env:MSTR_BASE_URL = if ($env:MSTR_BASE_URL) { $env:MSTR_BASE_URL } else { $env:MSTR_BASE_URL }
$env:MSTR_SOURCE_ENVIRONMENT = if ($env:MSTR_SOURCE_ENVIRONMENT) { $env:MSTR_SOURCE_ENVIRONMENT } else { 'dev' }
$env:MSTR_USERNAME = if ($env:MSTR_USERNAME) { $env:MSTR_USERNAME } else { 'mstr' }
$env:MSTR_PASSWORD = if ($env:MSTR_PASSWORD) { $env:MSTR_PASSWORD } else { '' }
$env:MSTR_PROJECT_ID = if ($env:MSTR_PROJECT_ID) { $env:MSTR_PROJECT_ID } else { '' }
$env:MSTR_DATASOURCE_INSTANCE_ID = if ($env:MSTR_DATASOURCE_INSTANCE_ID) { $env:MSTR_DATASOURCE_INSTANCE_ID } else { '' }

if (-not (Test-Path '.\.venv\Scripts\python.exe')) {
    throw "Python virtual environment not found at .\.venv\Scripts\python.exe"
}

& '.\.venv\Scripts\python.exe' 'import_package.py'
exit $LASTEXITCODE

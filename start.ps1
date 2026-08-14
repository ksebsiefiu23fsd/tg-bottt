$ErrorActionPreference = "Stop"

if (-not (Test-Path .\.venv311\Scripts\python.exe)) {
    throw "Run .\setup.ps1 first"
}
if (-not (Test-Path .env)) {
    throw ".env is missing. Copy .env.example to .env and add the token."
}

& .\.venv311\Scripts\python.exe bot.py

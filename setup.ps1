$ErrorActionPreference = "Stop"

$pythonCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    "python3.11",
    "python"
)

$pythonExecutable = $null
foreach ($candidate in $pythonCandidates) {
    try {
        $version = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -eq "3.11") {
            $pythonExecutable = $candidate
            break
        }
    } catch {
        continue
    }
}

if (-not $pythonExecutable) {
    throw "Python 3.11 is required. Install it and run setup.ps1 again."
}

& $pythonExecutable -m venv .venv311
if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment" }
& .\.venv311\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip" }
& .\.venv311\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Failed to install dependencies" }

& .\.venv311\Scripts\python.exe setup_argos.py
if ($LASTEXITCODE -ne 0) { throw "Failed to install the Argos en-ru translation model" }

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}

Write-Host "Ready. Add BOT_TOKEN to .env, then run .\start.ps1"

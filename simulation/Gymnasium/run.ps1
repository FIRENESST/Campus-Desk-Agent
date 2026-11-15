param(
    [string]$ListenHost = "127.0.0.1",
    [int]$Port = 19001
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    python -m venv (Join-Path $Root ".venv")
    & $Python -m pip install -r (Join-Path $Root "requirements.txt")
}

& $Python -m uvicorn app:app --host $ListenHost --port $Port

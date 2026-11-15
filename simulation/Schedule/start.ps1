$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& cargo run
exit $LASTEXITCODE

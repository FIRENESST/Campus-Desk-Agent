$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& cargo test
exit $LASTEXITCODE

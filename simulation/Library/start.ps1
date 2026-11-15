$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $root "compile.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location $root
try {
    & java -cp (Join-Path $root "out\classes") library.LibraryServer
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode

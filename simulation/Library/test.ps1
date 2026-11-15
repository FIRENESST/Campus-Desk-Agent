$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$classes = Join-Path $root "out\test-classes"
New-Item -ItemType Directory -Path $classes -Force | Out-Null
$sources = Get-ChildItem -Path (Join-Path $root "src"), (Join-Path $root "test") -Recurse -Filter "*.java" | Select-Object -ExpandProperty FullName
& javac -encoding UTF-8 -d $classes $sources
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location $root
try {
    foreach ($test in @("JsonTest", "LibraryStoreTest", "HttpIntegrationTest", "FrontendIntegrationTest")) {
        & java -ea -cp $classes "library.$test"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
} finally {
    Pop-Location
}
Write-Host "全部测试通过。"

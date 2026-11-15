$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$classes = Join-Path $root "out\classes"
New-Item -ItemType Directory -Path $classes -Force | Out-Null
$sources = Get-ChildItem -Path (Join-Path $root "src") -Recurse -Filter "*.java" | Select-Object -ExpandProperty FullName
& javac -encoding UTF-8 -d $classes $sources
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "编译完成：$classes"

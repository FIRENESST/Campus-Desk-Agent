$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PgBin = if ($env:MPM_POSTGRES_BIN) { $env:MPM_POSTGRES_BIN } else { Join-Path $Root "pgsql\bin" }
$PgCtl = Join-Path $PgBin "pg_ctl.exe"
$Data = Join-Path $Root "data"

if (-not (Test-Path -LiteralPath $Data)) {
    throw "数据库数据目录不存在：$Data"
}

& $PgCtl -D $Data status | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Output "PostgreSQL 未运行。"
    exit 0
}

& $PgCtl -D $Data -m fast -w stop
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL 停止失败。"
}

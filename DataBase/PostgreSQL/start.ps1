param([int]$Port = 55432)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PgBin = if ($env:MPM_POSTGRES_BIN) { $env:MPM_POSTGRES_BIN } else { Join-Path $Root "pgsql\bin" }
$PgCtl = Join-Path $PgBin "pg_ctl.exe"
$PgIsReady = Join-Path $PgBin "pg_isready.exe"
$Data = Join-Path $Root "data"
$LogDirectory = Join-Path $Root "logs"
$LogFile = Join-Path $LogDirectory "postgres.log"

if (-not (Test-Path -LiteralPath $Data)) {
    throw "数据库数据目录不存在：$Data"
}

& $PgCtl -D $Data status | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Output "PostgreSQL 已在运行。"
    exit 0
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
& $PgCtl -D $Data -l $LogFile -w start
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL 启动失败，请查看 $LogFile"
}

& $PgIsReady -h 127.0.0.1 -p $Port -d cloud_agent -U agent_app
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL 已启动，但 cloud_agent 尚未接受连接。"
}

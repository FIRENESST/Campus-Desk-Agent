$ErrorActionPreference = "Stop"
$runDirectory = Join-Path $PSScriptRoot ".run"
$pidFile = Join-Path $runDirectory "processes.json"

if (-not (Test-Path $pidFile)) {
    Write-Host "没有由 start_all.ps1 启动的模拟环境。"
    exit 0
}

$processes = ConvertFrom-Json (Get-Content -Path $pidFile -Raw)

function Stop-ProcessTree {
    param([int]$TargetId)

    foreach ($child in Get-CimInstance Win32_Process -Filter "ParentProcessId=$TargetId" -ErrorAction SilentlyContinue) {
        Stop-ProcessTree -TargetId ([int]$child.ProcessId)
    }
    Stop-Process -Id $TargetId -Force -ErrorAction SilentlyContinue
}

foreach ($item in $processes) {
    Stop-ProcessTree -TargetId ([int]$item.pid)
}
Remove-Item -Path $pidFile -Force
Write-Host "已停止五个后端及 MINTA 网关。"

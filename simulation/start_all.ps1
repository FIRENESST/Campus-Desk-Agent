param(
    [string]$MintaGw = (Join-Path $PSScriptRoot "..\MlNTA\core\target\debug\mintagw.exe")
)

$ErrorActionPreference = "Stop"
$runDirectory = Join-Path $PSScriptRoot ".run"
$pidFile = Join-Path $runDirectory "processes.json"
$config = Join-Path $PSScriptRoot "mintagw.toml"

if (Test-Path $pidFile) {
    throw "检测到已启动的模拟环境；请先运行 stop_all.ps1。"
}
if (-not (Test-Path $MintaGw)) {
    throw "找不到 mintagw：$MintaGw。请先构建 MlNTA，或通过 -MintaGw 指定程序。"
}

New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$started = [System.Collections.Generic.List[object]]::new()

function Start-OwnedProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runDirectory "$Name.out.log") `
        -RedirectStandardError (Join-Path $runDirectory "$Name.err.log") `
        -PassThru
    $started.Add([pscustomobject]@{ name = $Name; pid = $process.Id })
}

function Stop-ProcessTree {
    param([int]$TargetId)

    foreach ($child in Get-CimInstance Win32_Process -Filter "ParentProcessId=$TargetId" -ErrorAction SilentlyContinue) {
        Stop-ProcessTree -TargetId ([int]$child.ProcessId)
    }
    Stop-Process -Id $TargetId -Force -ErrorAction SilentlyContinue
}

function Stop-OwnedProcesses {
    foreach ($item in $started) {
        Stop-ProcessTree -TargetId ([int]$item.pid)
    }
}

function Wait-HttpService {
    param([string]$Name, [string]$Uri)

    foreach ($attempt in 1..80) {
        try {
            $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 1 -UseBasicParsing
            if ($response.StatusCode -eq 200) {
                return
            }
        } catch {
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name 未能就绪：$Uri；请查看 .run\$Name.err.log。"
}

try {
    $backends = @(
        @{ name = "gymnasium"; directory = "Gymnasium"; script = "run.ps1"; url = "http://127.0.0.1:19001/healthz" },
        @{ name = "club"; directory = "Club"; script = "server.js"; url = "http://127.0.0.1:19002/healthz" },
        @{ name = "library"; directory = "Library"; script = "start.ps1"; url = "http://127.0.0.1:19003/healthz" },
        @{ name = "classroom"; directory = "Classroom"; script = "start.ps1"; url = "http://127.0.0.1:19004/healthz" },
        @{ name = "schedule"; directory = "Schedule"; script = "start.ps1"; url = "http://127.0.0.1:19005/healthz" }
    )

    foreach ($backend in $backends) {
        $directory = Join-Path $PSScriptRoot $backend.directory
        if ($backend.name -eq "club") {
            Start-OwnedProcess -Name $backend.name -FilePath "node.exe" `
                -Arguments @($backend.script) -WorkingDirectory $directory
        } else {
            $script = Join-Path $directory $backend.script
            Start-OwnedProcess -Name $backend.name -FilePath "powershell.exe" `
                -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$script`"") `
                -WorkingDirectory $directory
        }
        Wait-HttpService -Name $backend.name -Uri $backend.url
    }

    Start-OwnedProcess -Name "mintagw" -FilePath (Resolve-Path $MintaGw).Path `
        -Arguments @("run", "--config", "`"$config`"") -WorkingDirectory $PSScriptRoot
    Wait-HttpService -Name "mintagw" -Uri "http://127.0.0.1:19000/healthz"

    $started | ConvertTo-Json | Set-Content -Path $pidFile -Encoding UTF8
    Write-Host "五个独立后端及 MINTA 网关已启动。"
    Write-Host "健身房：http://127.0.0.1:19001  社团：http://127.0.0.1:19002"
    Write-Host "图书馆：http://127.0.0.1:19003  教室：http://127.0.0.1:19004"
    Write-Host "查课表：http://127.0.0.1:19005"
    Write-Host "运维管理台：各服务 /admin"
    Write-Host "统一入口：http://127.0.0.1:19000/v1/call"
    Write-Host "停止环境：.\stop_all.ps1"
} catch {
    Stop-OwnedProcesses
    throw
}

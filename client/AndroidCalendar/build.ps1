[CmdletBinding()]
param(
    [string]$SdkRoot = $env:ANDROID_HOME,
    [string]$JdkHome = $env:JAVA_HOME,
    [switch]$InitializeSigning
)

$ErrorActionPreference = 'Stop'
if (!$SdkRoot) { $SdkRoot = Join-Path $PSScriptRoot '.sdk' }
$sdk = (Resolve-Path -LiteralPath $SdkRoot).Path
$platform = Join-Path $sdk 'platforms/android-35/android.jar'
$tools = Join-Path $sdk 'build-tools/35.0.0'
if (!(Test-Path -LiteralPath $platform)) { $platform = Join-Path $sdk 'android-35/android.jar' }
if (!(Test-Path -LiteralPath $tools)) { $tools = Join-Path $sdk 'android-15' }
if (!(Test-Path -LiteralPath $platform) -or !(Test-Path -LiteralPath (Join-Path $tools 'aapt2.exe'))) {
    throw '需要 Android SDK Platform 35 和 Build Tools 35.0.0。'
}

function JavaTool([string]$Name) {
    if ($JdkHome) { return Join-Path $JdkHome "bin/$Name.exe" }
    return (Get-Command "$Name.exe" -ErrorAction Stop).Source
}

function Run([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "构建失败：$(Split-Path $Executable -Leaf) 返回 $LASTEXITCODE。" }
}

$java = JavaTool 'java'
$javac = JavaTool 'javac'
$jar = JavaTool 'jar'
$signing = Join-Path $PSScriptRoot '.signing'
$store = Join-Path $signing 'campus-calendar.p12'
$password = Join-Path $signing 'password.dpapi'
$alias = 'campus-calendar'
$passwordVariable = 'CAMPUS_CALENDAR_BUILD_KEY_PASSWORD'
$oldPassword = [Environment]::GetEnvironmentVariable($passwordVariable, 'Process')
$buildRoot = (New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'build') -Force).FullName
$work = (New-Item -ItemType Directory -Path (Join-Path $buildRoot ([guid]::NewGuid().ToString('N')))).FullName
$staged = $null
try {
    if (!(Test-Path -LiteralPath $store) -and !(Test-Path -LiteralPath $password) -and $InitializeSigning) {
        New-Item -ItemType Directory -Path $signing -Force | Out-Null
        $random = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
        [Environment]::SetEnvironmentVariable($passwordVariable, $random, 'Process')
        Run (JavaTool 'keytool') @('-genkeypair', '-storetype', 'PKCS12', '-keystore', $store,
            "-storepass:env", $passwordVariable, '-alias', $alias, '-keyalg', 'RSA', '-keysize', '3072',
            '-validity', '10000', '-dname', 'CN=Campus Desk Calendar')
        ConvertTo-SecureString $random -AsPlainText -Force | ConvertFrom-SecureString | Set-Content -LiteralPath $password
        $random = $null
    }
    if (!(Test-Path -LiteralPath $store) -or !(Test-Path -LiteralPath $password)) {
        throw '签名资料缺失。首次构建使用 -InitializeSigning，升级时请使用原签名资料。'
    }
    $secret = ConvertTo-SecureString (Get-Content -LiteralPath $password -Raw).Trim()
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        [Environment]::SetEnvironmentVariable($passwordVariable, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer), 'Process')
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }

    $aapt = Join-Path $tools 'aapt2.exe'
    $compiled = Join-Path $work 'compiled.zip'
    $unsigned = Join-Path $work 'unsigned.apk'
    $aligned = Join-Path $work 'aligned.apk'
    $classes = (New-Item -ItemType Directory -Path (Join-Path $work 'classes')).FullName
    $dex = (New-Item -ItemType Directory -Path (Join-Path $work 'dex')).FullName
    $archive = Join-Path $work 'classes.jar'
    $sources = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'src') -Recurse -File -Filter '*.java' | ForEach-Object FullName)
    Run $aapt @('compile', '--dir', (Join-Path $PSScriptRoot 'res'), '-o', $compiled)
    Run $aapt @('link', '-I', $platform, '--manifest', (Join-Path $PSScriptRoot 'AndroidManifest.xml'),
        '--min-sdk-version', '26', '--target-sdk-version', '35', '-o', $unsigned, $compiled)
    Run $javac (@('-encoding', 'UTF-8', '--release', '8', '-classpath', $platform, '-d', $classes) + $sources)
    Run $jar @('--create', '--file', $archive, '-C', $classes, '.')
    Run $java @('-cp', (Join-Path $tools 'lib/d8.jar'), 'com.android.tools.r8.D8',
        '--min-api', '26', '--lib', $platform, '--output', $dex, $archive)
    Run $jar @('--update', '--file', $unsigned, '-C', $dex, 'classes.dex')
    Run (Join-Path $tools 'zipalign.exe') @('-f', '4', $unsigned, $aligned)

    $destination = (New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot '../../server/AndroidAppServer/releases') -Force).FullName
    $staged = Join-Path $destination ([guid]::NewGuid().ToString('N') + '.apk')
    $signer = Join-Path $tools 'lib/apksigner.jar'
    Run $java @('-jar', $signer, 'sign', '--ks', $store, '--ks-key-alias', $alias, '--ks-pass', "env:$passwordVariable",
        '--v4-signing-enabled', 'false', '--out', $staged, $aligned)
    Run $java @('-jar', $signer, 'verify', '--verbose', $staged)
    $release = Join-Path $destination 'cuc-campus-calendar.apk'
    [IO.File]::Move($staged, $release, $true)
    Write-Output $release
} finally {
    [Environment]::SetEnvironmentVariable($passwordVariable, $oldPassword, 'Process')
    if ($staged -and (Test-Path -LiteralPath $staged)) { Remove-Item -LiteralPath $staged }
    $resolvedWork = (Resolve-Path -LiteralPath $work).Path
    $resolvedBuild = (Resolve-Path -LiteralPath $buildRoot).Path
    if ((Split-Path -Parent $resolvedWork) -eq $resolvedBuild -and (Split-Path -Leaf $resolvedWork) -match '^[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force
    }
}

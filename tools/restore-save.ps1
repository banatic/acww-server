[CmdletBinding()]
param(
    [int]$Version = 0,
    [string]$IniPath = "",
    [switch]$Force,
    [switch]$KeepLocalSave
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# `$PSScriptRoot` is not populated while Windows PowerShell 5.1 binds parameter defaults.
if (-not $IniPath) { $IniPath = Join-Path $PSScriptRoot "acww-online.ini" }

# Windows PowerShell 5.1 may otherwise negotiate an obsolete TLS version.
if ([Net.ServicePointManager]::SecurityProtocol -band [Net.SecurityProtocolType]::Tls12) {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} else {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}

function Read-AcwwIni {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "INI 파일을 찾을 수 없습니다: $Path`n이 스크립트를 acww-online.ini와 같은 폴더에 두거나 -IniPath를 지정하세요."
    }

    $values = @{}
    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#") -or $line.StartsWith(";")) {
            continue
        }
        $equals = $line.IndexOf("=")
        if ($equals -lt 1) {
            continue
        }
        $key = $line.Substring(0, $equals).Trim().ToLowerInvariant()
        $value = $line.Substring($equals + 1).Trim()
        $values[$key] = $value
    }
    return $values
}

function Get-HttpStatus {
    param($ErrorRecord)
    try {
        return [int]$ErrorRecord.Exception.Response.StatusCode
    } catch {
        return 0
    }
}

function Get-HttpDetail {
    param($ErrorRecord)
    if ($ErrorRecord.ErrorDetails -and $ErrorRecord.ErrorDetails.Message) {
        try {
            $parsed = $ErrorRecord.ErrorDetails.Message | ConvertFrom-Json
            if ($parsed.detail) { return [string]$parsed.detail }
        } catch {
            return [string]$ErrorRecord.ErrorDetails.Message
        }
    }
    return [string]$ErrorRecord.Exception.Message
}

function Invoke-AcwwLogin {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Username
    )

    Write-Host "저장된 토큰이 없거나 만료되었습니다. 비밀번호는 디스크에 저장하지 않습니다."
    $credential = Get-Credential -UserName $Username -Message "ACWW 온라인 계정 로그인"
    if (-not $credential) { throw "로그인이 취소되었습니다." }
    $body = @{
        username = $credential.UserName
        password = $credential.GetNetworkCredential().Password
    } | ConvertTo-Json -Compress
    try {
        $reply = Invoke-RestMethod -UseBasicParsing -Uri "$BaseUrl/v1/auth/login" `
            -Method Post -ContentType "application/json; charset=utf-8" -Body $body
    } catch {
        throw "로그인 실패: $(Get-HttpDetail $_)"
    } finally {
        $body = $null
        $credential = $null
    }
    if (-not $reply.token) { throw "서버 로그인 응답에 token이 없습니다." }
    return [string]$reply.token
}

try {
    $running = @(Get-Process -Name "acww" -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        throw "실행 중인 acww.exe가 있습니다. 세이브 동기화를 막기 위해 모든 게임 창을 닫고 다시 실행하세요."
    }

    $IniPath = [IO.Path]::GetFullPath($IniPath)
    $ini = Read-AcwwIni -Path $IniPath
    if (-not $ini.ContainsKey("server") -or -not $ini["server"]) {
        throw "acww-online.ini에 server= 항목이 없습니다."
    }
    if (-not $ini.ContainsKey("username") -or -not $ini["username"]) {
        throw "acww-online.ini에 username= 항목이 없습니다."
    }

    $base = ([string]$ini["server"]).TrimEnd("/")
    $username = [string]$ini["username"]
    $token = if ($ini.ContainsKey("token")) { [string]$ini["token"] } else { "" }

    $me = $null
    if ($token) {
        try {
            $me = Invoke-RestMethod -UseBasicParsing -Uri "$base/v1/me" `
                -Headers @{ Authorization = "Bearer $token" }
        } catch {
            if ((Get-HttpStatus $_) -ne 401) {
                throw "서버 연결 실패: $(Get-HttpDetail $_)"
            }
            $token = ""
        }
    }
    if (-not $token) {
        $token = Invoke-AcwwLogin -BaseUrl $base -Username $username
        $me = Invoke-RestMethod -UseBasicParsing -Uri "$base/v1/me" `
            -Headers @{ Authorization = "Bearer $token" }
    }

    if (-not $me.save) { throw "이 계정에는 서버 세이브가 아직 없습니다." }
    $headers = @{ Authorization = "Bearer $token" }
    try {
        $historyReply = Invoke-RestMethod -UseBasicParsing -Uri "$base/v1/save/history" `
            -Headers $headers
        $history = if ($historyReply -is [System.Array]) { $historyReply } else { @($historyReply) }
    } catch {
        throw "세이브 이력을 가져오지 못했습니다: $(Get-HttpDetail $_)"
    }
    if ($history.Count -eq 0) { throw "서버가 빈 세이브 이력을 반환했습니다." }

    Write-Host ""
    Write-Host "계정: $($me.username)    현재 최신 버전: $($me.save.version)"
    $history | Select-Object version, updated_utc, size,
        @{Name = "sha256"; Expression = { ([string]$_.sha256).Substring(0, 16) + "..." }} |
        Format-Table -AutoSize

    if ($Version -le 0) {
        $answer = Read-Host "복원할 이전 버전 번호"
        if (-not [int]::TryParse($answer, [ref]$Version) -or $Version -le 0) {
            throw "올바른 버전 번호가 아닙니다: $answer"
        }
    }

    $targetRows = @($history | Where-Object { [int]$_.version -eq $Version })
    if ($targetRows.Count -ne 1) {
        throw "보관 이력에 버전 $Version 이 없습니다. 서버는 기본적으로 최근 20개만 보관합니다."
    }
    $target = $targetRows[0]
    $currentVersion = [int]$me.save.version
    if ($Version -eq $currentVersion) {
        throw "버전 $Version 은 이미 최신 버전입니다. 더 이전 버전을 선택하세요."
    }

    Write-Host "복원 대상: 버전 $Version / $($target.updated_utc) / $($target.sha256)"
    Write-Host "현재 버전 $currentVersion 은 삭제되지 않고 서버 이력에 남습니다."
    if (-not $Force) {
        $confirm = Read-Host "계속하려면 ROLLBACK 을 입력"
        if ($confirm -cne "ROLLBACK") {
            Write-Host "취소했습니다. 서버와 로컬 세이브는 변경되지 않았습니다."
            exit 0
        }
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $download = Join-Path $PSScriptRoot ".rollback-v$Version-$stamp.download"
    $receipt = Join-Path $PSScriptRoot "rollback-v$Version-$stamp.sav"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$base/v1/save/$Version" `
            -Headers $headers -OutFile $download
        $file = Get-Item -LiteralPath $download
        if ($file.Length -ne 262144) {
            throw "다운로드 크기가 262,144바이트가 아닙니다: $($file.Length)"
        }
        $actualHash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
        $expectedHash = ([string]$target.sha256).ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            throw "다운로드 SHA-256 불일치: expected=$expectedHash actual=$actualHash"
        }
        Move-Item -LiteralPath $download -Destination $receipt
    } finally {
        if (Test-Path -LiteralPath $download) {
            Remove-Item -LiteralPath $download -Force
        }
    }

    # Optimistic concurrency keeps a client upload racing this script from being overwritten.
    $uploadHeaders = @{
        Authorization = "Bearer $token"
        "If-Match" = '"{0}"' -f $currentVersion
    }
    try {
        $restored = Invoke-RestMethod -UseBasicParsing -Uri "$base/v1/save" `
            -Method Put -Headers $uploadHeaders -ContentType "application/octet-stream" `
            -InFile $receipt
    } catch {
        throw "복원 업로드 실패: $(Get-HttpDetail $_)`n검증된 파일은 보존했습니다: $receipt"
    }

    $localSave = Join-Path $PSScriptRoot "acww.sav"
    $localBackup = $null
    if ((Test-Path -LiteralPath $localSave -PathType Leaf) -and -not $KeepLocalSave) {
        $localBackup = Join-Path $PSScriptRoot "acww.sav.before-rollback-$stamp.bak"
        try {
            Move-Item -LiteralPath $localSave -Destination $localBackup
        } catch {
            Write-Warning "서버 복원은 성공했지만 로컬 acww.sav를 옮기지 못했습니다: $($_.Exception.Message)"
            Write-Warning "게임 실행 전에 acww.sav를 수동으로 다른 이름으로 바꾸세요."
        }
    }

    Write-Host ""
    Write-Host "복원 성공: 서버 버전 $Version -> 새 최신 버전 $($restored.version)"
    Write-Host "복원 원본 보관: $receipt"
    if ($localBackup) { Write-Host "기존 로컬 세이브 보관: $localBackup" }
    if ($KeepLocalSave -and (Test-Path -LiteralPath $localSave)) {
        Write-Warning "-KeepLocalSave가 지정되어 로컬 acww.sav를 유지했습니다. 동기화 질문에서는 서버 세이브를 선택하세요."
    } else {
        Write-Host "이제 acww.exe를 실행하고 로그인하면 복원된 서버 세이브를 내려받습니다."
    }
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}

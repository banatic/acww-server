<#
.SYNOPSIS
  동물의 숲 서버에 계정을 만듭니다 (관리자 전용, 클로즈드 베타).

.DESCRIPTION
  서버의 회원가입(ACWW_ALLOW_REGISTER=0)을 닫아 둔 채로, 관리자 토큰으로 계정을 만듭니다.
  POST /v1/admin/users (WEB05, server/app/main.py) 를 부릅니다.

  관리자 토큰은 다음 순서로 찾습니다 (README-ko.md "관리자" 절과 같음):
    1. 환경 변수 ACWW_ADMIN_TOKEN
    2. %USERPROFILE%\.acww-admin-token 파일 (NAS 의 acww-online/data/admin.key 내용)

  비밀번호는 화면에 보이지 않게 두 번 입력받고, 명령줄 인자로는 받지 않습니다
  (셸 기록과 프로세스 목록에 남지 않게).

  세이브 옮기기: -Save 로 .sav 파일을 주거나, 물어볼 때 y 를 고르면 파일 창이 뜹니다.
  고른 세이브는 그 계정의 첫 클라우드 세이브(버전 1)가 되어, 처음 로그인할 때 그 마을로
  시작합니다. PUT /v1/admin/users/{아이디}/save 로 올리고, 서버와 같은 검사(한국판
  세이브인지, 두 뱅크의 체크섬)를 계정을 만들기 전에 먼저 해서, 못 쓰는 파일이면 계정도
  만들지 않습니다. 262,144 바이트 카드 이미지(이 게임 EXE 의 세이브, 대부분의 에뮬레이터
  .sav)와 DeSmuME .dsv(뒤의 꼬리 정보는 떼어 냄)를 받습니다.

.EXAMPLE
  .\add-user.ps1
  .\add-user.ps1 -Username friend_01
  .\add-user.ps1 -Username friend_01 -Save "D:\saves\animal crossing.sav"
  .\add-user.ps1 -Username friend_02 -NoSave
  .\add-user.ps1 -Url http://127.0.0.1:8080 -Username tester
#>
[CmdletBinding()]
param(
    [string]$Username = "",
    [string]$Url = "https://acww.moominda.synology.me",
    [string]$Save = "",
    [switch]$NoSave
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# Windows PowerShell 5.1 may otherwise negotiate an obsolete TLS version.
[Net.ServicePointManager]::SecurityProtocol =
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Get-AdminToken {
    if ($env:ACWW_ADMIN_TOKEN) { return $env:ACWW_ADMIN_TOKEN.Trim() }
    $file = Join-Path $env:USERPROFILE ".acww-admin-token"
    if (Test-Path -LiteralPath $file -PathType Leaf) {
        $t = (Get-Content -LiteralPath $file -Raw -Encoding UTF8).Trim()
        if ($t) { return $t }
    }
    throw "관리자 토큰이 없습니다. NAS 의 acww-online/data/admin.key 내용을 $file 에 붙여 넣으세요."
}

function Read-Plain([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

# ---- the save to migrate: read and checked BEFORE the account exists -------------------
$FlashSize = 0x40000; $BankSize = 0x173FC

function Read-CardImage([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "파일이 없어요: $Path" }
    $b = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Path).ProviderPath)
    if ($b.Length -gt $FlashSize) {
        $tail = $b[$FlashSize..($b.Length - 1)]
        $text = [Text.Encoding]::ASCII.GetString($tail)
        $blank = -not ($tail | Where-Object { $_ -ne 0xFF } | Select-Object -First 1)
        if ($text -like "*DESMUME*" -or $text.StartsWith("|<--Snip") -or $blank) {
            $b = [byte[]]$b[0..($FlashSize - 1)]     # DeSmuME footer, or a 512 KB dump of a 256 KB chip
        }
    }
    if ($b.Length -ne $FlashSize) {
        throw ("세이브는 262,144 바이트 카드 이미지여야 해요 (이 파일은 {0:N0} 바이트)." -f $b.Length)
    }
    # The ROM's own test (server/app/savecheck.py, func_020a1a40), on both banks.
    foreach ($off in 0, $BankSize) {
        $name = if ($off) { "두 번째 뱅크" } else { "첫 번째 뱅크" }
        if ($b[$off] -ne 0x32) { throw "${name}가 한국판 세이브가 아니에요 (다른 지역판이거나 빈 세이브)." }
        if ($b[$off + 0x173FA] -ne 2) { throw "${name}가 저장이 끝난 세이브가 아니에요." }
        $sum = 0
        # ToUInt16, not `$b[$i+1] -shl 8`: a [byte] shifted stays a byte (the bits fall off).
        for ($i = $off; $i -lt $off + $BankSize; $i += 2) { $sum = ($sum + [BitConverter]::ToUInt16($b, $i)) -band 0xFFFF }
        if ($sum -ne 0) { throw "${name}의 체크섬이 맞지 않아요 (깨진 세이브)." }
    }
    return ,$b
}

function Select-SaveFile {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $d = New-Object System.Windows.Forms.OpenFileDialog
        $d.Title = "옮길 세이브 파일"
        $d.Filter = "세이브 (*.sav;*.dsv;*.bin)|*.sav;*.dsv;*.bin|모든 파일 (*.*)|*.*"
        if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $d.FileName }
        return ""
    } catch {
        return (Read-Host -Prompt "세이브 파일 경로").Trim('"', ' ')
    }
}

$saveBytes = $null
if (-not $NoSave) {
    if (-not $Save) {
        $answer = (Read-Host -Prompt "기존 세이브를 옮길까요? 옮기면 그게 첫 세이브가 돼요 (y/N)").Trim()
        if ($answer -match '^(y|yes|ㅛ|네|예)$') { $Save = Select-SaveFile }
    }
    if ($Save) {
        try { $saveBytes = Read-CardImage $Save }
        catch { Write-Host "세이브를 쓸 수 없어요: $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
        Write-Host ("세이브 확인됨: {0}" -f $Save) -ForegroundColor Green
    }
}

$token = Get-AdminToken

while (-not ($Username -match '^[A-Za-z0-9_]{3,24}$')) {
    if ($Username) { Write-Host "아이디는 영문/숫자/_ 3~24자여야 해요." -ForegroundColor Yellow }
    $Username = (Read-Host -Prompt "새 아이디").Trim()
}

while ($true) {
    $p1 = Read-Plain "비밀번호 (8자 이상)"
    if ($p1.Length -lt 8) { Write-Host "비밀번호는 8자 이상이어야 해요." -ForegroundColor Yellow; continue }
    $p2 = Read-Plain "비밀번호 다시"
    if ($p1 -ne $p2) { Write-Host "두 비밀번호가 달라요. 다시 입력해 주세요." -ForegroundColor Yellow; continue }
    break
}

$body = @{ username = $Username; password = $p1 } | ConvertTo-Json -Compress
$p1 = $null; $p2 = $null
$endpoint = $Url.TrimEnd("/") + "/v1/admin/users"

try {
    $r = Invoke-RestMethod -Method Post -Uri $endpoint -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
        -ContentType "application/json; charset=utf-8" -Headers @{ "X-Admin-Token" = $token }
    Write-Host ("계정을 만들었어요: {0} (user_id {1})" -f $r.username, $r.user_id) -ForegroundColor Green
    if ($saveBytes) {
        try {
            $s = Invoke-RestMethod -Method Put -Uri ($Url.TrimEnd("/") + "/v1/admin/users/" +
                    [Uri]::EscapeDataString($Username) + "/save") -Body $saveBytes `
                -ContentType "application/octet-stream" -Headers @{ "X-Admin-Token" = $token }
            Write-Host ("세이브를 올렸어요 (버전 {0}). 첫 로그인 때 이 마을로 시작해요." -f $s.version) -ForegroundColor Green
        } catch {
            $why = ""
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                try { $why = ($_.ErrorDetails.Message | ConvertFrom-Json).detail } catch { $why = $_.ErrorDetails.Message }
            }
            Write-Host "계정은 만들어졌지만 세이브는 못 올렸어요: $why $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "그 아이디로 처음 로그인하기 전에 다시 올려야 해요." -ForegroundColor Yellow
            exit 2
        }
    }
} catch {
    $status = 0
    try { $status = [int]$_.Exception.Response.StatusCode } catch { }
    $detail = ""
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        try { $detail = ($_.ErrorDetails.Message | ConvertFrom-Json).detail } catch { $detail = $_.ErrorDetails.Message }
    }
    switch ($status) {
        409 { Write-Host "이미 있는 아이디예요: $Username" -ForegroundColor Red }
        401 { Write-Host "관리자 토큰이 맞지 않아요." -ForegroundColor Red }
        404 { Write-Host "서버에 관리자 기능이 꺼져 있거나 오래된 서버예요 (/v1/admin/users 없음)." -ForegroundColor Red }
        400 { Write-Host "서버가 거절했어요: $detail" -ForegroundColor Red }
        429 { Write-Host "시도가 너무 많아요. 1분 뒤에 다시 해 주세요." -ForegroundColor Red }
        default { Write-Host "실패했어요 ($status): $detail $($_.Exception.Message)" -ForegroundColor Red }
    }
    exit 1
} finally {
    $body = $null
}

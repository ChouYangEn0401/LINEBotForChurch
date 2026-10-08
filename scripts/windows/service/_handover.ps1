#Requires -Version 5.1
<#
一次性的交接腳本：把「手動雙擊 2-start 開著的後台」換成「Windows 服務」。
註冊服務那一天跑一次就好，跑完可以刪掉。
（平常要停／開／重開，請用同一個資料夾裡的 stop.bat / start.bat / restart.bat。）
#>
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$Log = Join-Path $Root 'data\handover.log'
$nssm = Join-Path $Root 'tools\nssm.exe'

function Say([string]$text) {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $text"
    Write-Host $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

Say '=== 把後台從「手動 2-start」交接給 Windows 服務 ==='

Say '1. 先讓服務停下來（它正在不斷重試，因為 port 被舊的佔著）'
& $nssm stop church-bot | Out-Null
Start-Sleep -Seconds 2

Say '2. 收掉手動開著的 2-start（tunnel / 免費模式不動）'
$manual = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'church_bot' -and $_.CommandLine -match '\bweb\b' }
foreach ($p in $manual) {
    Say "   停止 pid $($p.ProcessId)"
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { Say "   （$($p.ProcessId) 已經不在了）" }
}

Say '3. 等 8787 放出來'
$free = $false
foreach ($i in 1..30) {
    $held = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
    if (-not $held) { $free = $true; break }
    Start-Sleep -Milliseconds 500
}
if (-not $free) { Say '   [X] 8787 還是被佔著，先停在這裡。'; Read-Host '按 Enter 關掉'; exit 1 }
Say '   8787 已經空出來'

Say '4. 啟動服務'
& $nssm start church-bot | Out-Null
Start-Sleep -Seconds 5

Say '5. 驗證'
$svc = Get-Service church-bot
Say "   服務狀態：$($svc.Status)"
$owner = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
if ($owner) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($owner[0].OwningProcess)"
    Say "   8787 現在是：$($proc.Name) pid=$($proc.ProcessId)"
    $root = $proc
    while ($root -and $root.Name -eq 'python.exe') {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($root.ParentProcessId)" -ErrorAction SilentlyContinue
        if (-not $parent) { break }
        $root = $parent
    }
    Say "   往上追到的根程序：$($root.Name) pid=$($root.ProcessId)  ← 要是 nssm.exe 才算接手成功"
} else {
    Say '   [X] 沒有人在聽 8787，服務裡的程式沒起來。看 data\service.log。'
}

Say '6. 讓 Windows 自己也保一層：nssm 掛掉時由服務控制器重開'
& sc.exe failure church-bot reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
& sc.exe qfailure church-bot

Write-Host ''
Read-Host '按 Enter 關掉這個視窗' | Out-Null

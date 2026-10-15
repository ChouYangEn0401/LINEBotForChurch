#Requires -Version 5.1
<#
把兩個常駐程式註冊成 Windows 服務，讓它們開機自己起來、當掉自己重開：

  web     church-bot           管理網頁後台（2-start.bat 做的事）：每週提醒就是它發的
  webhook church-bot-webhook   LINE 指令的臨時網址（3-open-webhook.bat 做的事）：開著時群組打 /提醒 才會回

一般人不用看這個檔案：雙擊 scripts\windows 裡的 2-service-*.bat、3-service-*.bat，
或這個資料夾裡的 install.bat / start.bat / stop.bat / restart.bat / status.bat / uninstall.bat
（這幾個是兩個一起），它們都只是呼叫這裡的某一個動作。右下角的小圖示也可以停／開／重開。

做法跟 nginx 在 Windows 上一樣：nginx、python 這種程式不會跟 Windows 的服務控制器
（SCM）對話，直接 sc create 會在啟動時卡住、跳 error 1053。所以中間包一層 nssm，
由 nssm 當服務、再由它去開我們的程式。包好之後 sc start/stop/query、服務管理員
（services.msc）、net start 全都通。

權限：註冊、移除一定要系統管理員（會跳一次 UAC）。註冊時順手把「啟動／停止／查詢」這兩個服務的權限
開給你自己的 Windows 帳號（只有這兩個服務），之後停／開／重開、右下角小圖示都不用再跳 UAC。

為什麼邏輯寫在 .ps1 不寫在 .bat：這些訊息是中文，而 cmd.exe 讀 UTF-8 的 .bat 時，
檔案一大就會在區塊邊界把多位元組字切斷、整行變成亂碼指令。.bat 只留純英文的一行呼叫。
這個檔案本身要存成「UTF-8 含 BOM」，不然 Windows PowerShell 5.1 會用 cp950 讀、中文全壞。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'start', 'stop', 'restart', 'status', 'uninstall')]
    [string]$Action,
    # 哪一個：web（後台）、webhook（LINE 指令）、all（兩個）。預設 web：不帶這個參數的呼叫
    # （ClawBot 的「L」小圖示、舊版的 .bat）一直以來指的都是 church-bot 這一個，不能變成兩個一起停
    [ValidateSet('all', 'web', 'webhook')]
    [string]$Target = 'web',
    # 由自己重新用系統管理員身分開起來的那一份會帶這個，用來避免無限重開
    [switch]$Elevated,
    # 要把服務權限開給誰（提升權限之前的那個人；同一個帳號的話就是自己）
    [string]$UserSid = '',
    # 自動測試用：跑完不要停在「按 Enter」
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$VenvPythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$TrayScript = Join-Path $Root 'scripts\windows\tray.pyw'
$NssmPath = Join-Path $Root 'tools\nssm.exe'
$TrayShortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'church-bot-tray.lnk'
if (-not $UserSid) { $UserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }

$Bots = [ordered]@{
    web     = @{ Name = 'church-bot'; Label = '後台（管理網頁＋每週提醒）'
                 Display = '教會服事提醒機器人'
                 Description = '每週自動發送 LINE 服事提醒。管理網頁：http://127.0.0.1:8787' }
    webhook = @{ Name = 'church-bot-webhook'; Label = 'LINE 指令（臨時網址）'
                 Display = '教會服事提醒機器人：LINE 指令'
                 Description = 'Cloudflare 臨時網址＋自動登記到 LINE；開著時群組打 /提醒、/我的名字 機器人才會回' }
}
$Keys = if ($Target -eq 'all') { @($Bots.Keys) } else { @($Target) }

function Write-Step([string]$text) { Write-Host "`n$text" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "[OK] $text" -ForegroundColor Green }
function Write-Bad([string]$text) { Write-Host "[X] $text" -ForegroundColor Red }

function Test-Admin {
    $me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Stop-Here([int]$code) {
    Write-Host ''
    if (-not $NoPause) { Read-Host '按 Enter 關掉這個視窗' | Out-Null }
    exit $code
}

function Invoke-Elevated {
    # 用系統管理員身分把同一個動作再跑一次，等它做完（那個視窗按 Enter 才會回來），回傳它的結束代碼
    if ($Elevated) {
        Write-Bad '沒有取得系統管理員權限，沒辦法繼續。'
        Stop-Here 1
    }
    Write-Host 'Windows 會問「是否允許這個 App 變更你的裝置」，請按「是」。'
    $psArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
                '-Action', $Action, '-Target', $Target, '-Elevated', '-UserSid', $UserSid)
    try {
        $proc = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $psArgs -Wait -PassThru
    } catch {
        Write-Bad '沒有取得系統管理員權限（按了「否」，或這個帳號不能提升權限）。'
        Write-Host '   也可以在「開始」搜尋 PowerShell，右鍵「以系統管理員身分執行」，再執行這個檔案。'
        Stop-Here 1
    }
    return $proc.ExitCode
}

# --------------------------------------------------------------------------- nssm

function Find-NssmSource {
    # winget 裝好之後，真正的 nssm.exe 在哪裡
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path $packages) {
        $hit = Get-ChildItem $packages -Filter 'nssm.exe' -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like '*win64*' } | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    $onPath = Get-Command 'nssm' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

function Get-Nssm([switch]$Fetch) {
    # 專案自己那一份優先：nssm.exe 註冊之後就是這個服務的執行檔本體，路徑以後不能變。
    # winget 裝的那一份在 AppData 裡、升級時資料夾名稱會換，拿它去註冊，哪天升級服務就壞了。
    if (Test-Path $NssmPath) { return $NssmPath }
    if (-not $Fetch) {
        Write-Bad '找不到 nssm：還沒註冊成服務。請先雙擊 scripts\windows\service\install.bat。'
        Stop-Here 1
    }
    Write-Step '這台電腦還沒有 nssm（把一般程式包成 Windows 服務的小工具），正在準備…'
    $source = Find-NssmSource
    if (-not $source) {
        # winget 的 NSSM.NSSM 是 portable（zip）、會驗發行者的 SHA256，而且版本是 2.24-101——
        # Windows 10 Creators Update 之後一定要這個版本以上，舊的 2.24 正式版註冊出來的服務起不來。
        Write-Host '正在用 winget 下載（整台電腦只要這一次）…'
        & winget install --id NSSM.NSSM --accept-source-agreements --accept-package-agreements --disable-interactivity
        $source = Find-NssmSource
    }
    if (-not $source) {
        Write-Bad '自動下載失敗。'
        Write-Host '   請自己到 https://nssm.cc 下載 2.24-101 以上的版本，'
        Write-Host "   把裡面 win64\nssm.exe 放到：$NssmPath"
        Write-Host '   然後重新雙擊 install.bat。'
        Stop-Here 1
    }
    New-Item -ItemType Directory -Force (Join-Path $Root 'tools') | Out-Null
    Copy-Item $source $NssmPath -Force
    Write-Ok "nssm 準備好了：$NssmPath"
    return $NssmPath
}

function Join-Arguments([string[]]$items) {
    # 自己組命令列、每個參數都加引號（裡面的 " 與結尾的 \ 照 Windows 的規則跳脫）。
    # Windows PowerShell 5.1 呼叫外部程式時不會好好處理：有空白的路徑被切成好幾段、參數裡的引號不見。
    ($items | ForEach-Object { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }) -join ' '
}
function Invoke-Nssm([string]$nssm, [string[]]$arguments) {
    # 2026-10-09：church-bot-webhook 的 AppParameters 是 --cloudflared "C:\Program Files (x86)\..."，
    # 用 & $nssm @arguments 交給 nssm 時 Windows PowerShell 5.1 把引號弄丟，路徑被空白切成三段，
    # 服務一啟動就 exit 2、被 nssm 一直重開（卡在 PAUSED）。所以命令列自己組（Join-Arguments），不靠 5.1。
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $nssm
    $psi.Arguments = Join-Arguments $arguments
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $text = $proc.StandardOutput.ReadToEnd() + $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    # nssm 的輸出是 UTF-16，讀回來會夾雜 \0；真正要看的是結束代碼
    return [pscustomobject]@{ Code = $proc.ExitCode; Text = (($text -replace "`0", '').Trim()) }
}

function Test-Denied([string]$text) {
    return $text -match 'Access is denied|拒絕存取|存取被拒'
}

function Get-ServiceState([string]$nssm, [string]$name) {
    $result = Invoke-Nssm $nssm @('status', $name)
    if ($result.Code -ne 0) { return $null }  # 沒註冊過
    return ($result.Text -replace '\0', '').Trim()
}

function Find-Cloudflared {
    $onPath = Get-Command 'cloudflared' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    foreach ($dir in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if ($dir -and (Test-Path "$dir\cloudflared\cloudflared.exe")) { return "$dir\cloudflared\cloudflared.exe" }
    }
    return $null
}

# --------------------------------------------------------------------------- 權限：開給自己的帳號

function Grant-Control([string]$name) {
    # 在服務的 DACL 加一條：這個帳號可以查詢（CC LC SW LO RC）、啟動（RP）、停止（WP）、暫停（DT）、
    # 送自訂控制（CR）。不給 WD／WO（改權限、改擁有者），所以拿不到更多。
    $ace = "(A;;CCLCSWRPWPDTLOCRRC;;;$UserSid)"
    $sddl = ((& sc.exe sdshow $name) -join '').Trim()
    if (-not $sddl.StartsWith('D:')) { Write-Bad "讀不到 $name 的權限設定：$sddl"; return }
    if ($sddl.Contains($ace)) { return }
    $at = $sddl.IndexOf('S:')
    $new = if ($at -gt 0) { $sddl.Substring(0, $at) + $ace + $sddl.Substring($at) } else { $sddl + $ace }
    $out = & sc.exe sdset $name $new
    if ($LASTEXITCODE -ne 0) { Write-Bad "開權限給你的帳號失敗（$name）：$out" }
}

# --------------------------------------------------------------------------- 右下角小圖示

function Install-TrayShortcut {
    # 登入 Windows 時開右下角的兩個小圖示（只開小圖示，不開程式本體，所以不會跟服務搶 port）
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($TrayShortcut)
    $link.TargetPath = $VenvPythonw
    $link.Arguments = "`"$TrayScript`""
    $link.WorkingDirectory = $Root
    $link.Description = '教會服事提醒機器人：右下角小圖示'
    $link.Save()
}

function Start-Tray {
    if (Test-Path $VenvPythonw) { Start-Process -FilePath $VenvPythonw -ArgumentList "`"$TrayScript`"" -WorkingDirectory $Root }
}

# --------------------------------------------------------------------------- 各個動作

function Set-Bot([string]$nssm, [string]$key, [string]$cloudflared) {
    $bot = $Bots[$key]
    $name = $bot.Name
    if ($null -eq (Get-ServiceState $nssm $name)) {
        Write-Step "正在註冊：$($bot.Label)（$name）…"
        $made = Invoke-Nssm $nssm @('install', $name, $VenvPython)
        if ($made.Code -ne 0) { Write-Bad "註冊失敗：$($made.Text)"; Stop-Here 1 }
    } else {
        Write-Step "$($bot.Label)（$name）已經存在，先停下來再重新設定（設定、名單、發送紀錄都不會動）…"
        Invoke-Nssm $nssm @('stop', $name) | Out-Null
    }

    $parameters = if ($key -eq 'web') { '-m church_bot web --no-browser' }
                  else { "-m church_bot tunnel --port 8787 --cloudflared `"$cloudflared`"" }
    $settings = @(
        @('Application', $VenvPython),
        @('AppParameters', $parameters),
        @('AppDirectory', $Root),
        # CHURCH_BOT_SERVICE=1：程式知道自己是服務（啟動通知會寫「以 Windows 服務執行」，小圖示會顯示綠色）
        @('AppEnvironmentExtra', "PYTHONPATH=$Root\src", 'PYTHONUTF8=1', 'PYTHONIOENCODING=utf-8',
          "CHURCH_BOT_HOME=$Root", 'CHURCH_BOT_SERVICE=1'),
        @('DisplayName', $bot.Display),
        @('Description', $bot.Description),
        # 後台：延遲自動啟動，開機後等網路就緒才起來，比較不會一開機就讀不到 Google 服事表。
        # LINE 指令：手動啟動（擁有者 2026-10-15）——要用才開（Telegram、小圖示「服務：啟動」），
        # 開幾個小時自己關（config/church.yaml 的 webhook.hours），開機不會自己起來
        @('Start', $(if ($key -eq 'web') { 'SERVICE_DELAYED_AUTO_START' } else { 'SERVICE_DEMAND_START' })),
        # 刻意不設 AppStdout／AppStderr（輸出重導向）：2026-10-09 實測，開了它加上線上輪替，
        # nssm 停服務時程式早就結束了，自己卻卡在等 pipe 讀完，服務一直停在「正在停止」。
        # 要看的紀錄程式自己會寫在 data\church_bot.log，不需要 nssm 再抄一份。
        @('AppRotateFiles', '0'), @('AppRotateOnline', '0'),
        # 停止時先送 Ctrl+C：程式接得住，會等正在發的那一則送完再收工。最多等 25 秒
        @('AppStopMethodConsole', '25000'),
        # 自己掛掉（或從小圖示「結束程式」）就等 10 秒再開一次；一直掛也至少隔 10 秒，不要瘋狂重開
        @('AppExit', 'Default', 'Restart'), @('AppRestartDelay', '10000'), @('AppThrottle', '10000')
    )
    # LINE 指令要等後台起來：網址是轉到後台的，後台沒開，登記到 LINE 也沒用
    if ($key -eq 'webhook') { $settings += , @('DependOnService', $Bots.web.Name) }
    foreach ($one in $settings) {
        $result = Invoke-Nssm $nssm (@('set', $name) + $one)
        if ($result.Code -ne 0) { Write-Bad "設定 $($one[0]) 失敗：$($result.Text)"; Stop-Here 1 }
    }
    # 舊版 install.bat 註冊過的服務有設輸出重導向（見上面），重新安裝時順手拿掉
    foreach ($p in @('AppStdout', 'AppStderr')) { Invoke-Nssm $nssm @('reset', $name, $p) | Out-Null }
    Grant-Control $name
}

function Install-Bots {
    $nssm = Get-Nssm -Fetch
    if (-not (Test-Path $VenvPython)) {
        Write-Bad '還沒安裝。請先雙擊 scripts\windows\1-install.bat'
        Stop-Here 1
    }
    Write-Host ''
    Write-Host "專案資料夾：$Root"
    Write-Host "nssm　　　：$nssm"

    $cloudflared = $null
    $todo = @($Keys)
    if ($todo -contains 'webhook') {
        $cloudflared = Find-Cloudflared
        if (-not $cloudflared) {
            Write-Bad '找不到 cloudflared，所以「LINE 指令」這個服務先不註冊（後台照樣註冊）。'
            Write-Host '   要的話先裝：winget install --id Cloudflare.cloudflared，再雙擊一次 install.bat。'
            $todo = @($todo | Where-Object { $_ -ne 'webhook' })
        } else {
            Write-Host "cloudflared：$cloudflared"
        }
    }
    if ($todo -contains 'webhook' -and $todo -notcontains 'web' -and $null -eq (Get-ServiceState $nssm $Bots.web.Name)) {
        Write-Bad 'LINE 指令要接在後台後面，後台還沒註冊。請用 install.bat（兩個一起）。'
        Stop-Here 1
    }

    # 雙擊開著的那一份要先關：後台會搶 port，臨時網址會兩條互相把對方從 LINE 擠掉。
    # 不幫你關（那是你的視窗），請你自己關，關好按 Enter。
    foreach ($key in $todo) {
        $pattern = if ($key -eq 'web') { 'church_bot\s+web\b' } else { 'church_bot\s+tunnel\b' }
        $launcher = if ($key -eq 'web') { '2-start' } else { '3-open-webhook' }
        while ($true) {
            $manual = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                Where-Object { $_.SessionId -ne 0 -and $_.CommandLine -match $pattern })
            if ($manual.Count -eq 0) { break }
            Write-Bad "$($Bots[$key].Label)現在是用雙擊 $launcher 開著的（黑色視窗）。"
            $answer = Read-Host "   請先關掉那個視窗，關好按 Enter（打 s 跳過，不建議）"
            if ($answer -match '^[Ss]') { break }
        }
    }

    foreach ($key in $todo) { Set-Bot $nssm $key $cloudflared }

    # 登入時自動執行 2-start 的舊捷徑會跟服務搶同一個 port，有的話順手拿掉
    $old = Join-Path ([Environment]::GetFolderPath('Startup')) 'church-bot.lnk'
    if (Test-Path $old) {
        Remove-Item $old
        Write-Host '（順手取消了舊的「開機自動執行 2-start」捷徑，改由服務負責）'
    }

    foreach ($key in $todo) {
        Write-Step "正在啟動：$($Bots[$key].Label)…"
        $started = Invoke-Nssm $nssm @('start', $Bots[$key].Name)
        if ($started.Code -ne 0) {
            Write-Bad "註冊好了，但啟動失敗：$($started.Text)"
            Write-Host '   最常見的原因是那個程式本來就開著（2-start／3-open-webhook 的黑色視窗）：'
            Write-Host '   先關掉那個視窗，再雙擊 service\start.bat。'
            Write-Host "   還是不行就看 $Root\data\church_bot.log。"
            Stop-Here 1
        }
    }

    Write-Host ''
    Write-Ok '完成。從現在起：'
    Write-Host '     ・後台：這台電腦重新開機後，會自己起來（不用登入 Windows）'
    Write-Host '     ・LINE 指令（臨時網址）：要用才開，開幾個小時自己關（預設 3 小時），開機不會自己起來'
    Write-Host '       開：右下角 Lw 小圖示「服務：啟動」、3-service-start.bat、或 Telegram 叫 ClawBot'
    Write-Host '     ・程式當掉的話，10 秒後自己再開一次'
    Write-Host '     ・右下角有小圖示（綠＝服務在跑），右鍵可以結束程式、停／開／重開服務'
    Write-Host '     ・管理網頁：http://127.0.0.1:8787'
    Write-Host ''
    Write-Host '     也可以雙擊 scripts\windows 裡的：'
    Write-Host '       2-service-start / 2-service-stop / 2-service-restart（後台）'
    Write-Host '       3-service-start / 3-service-stop / 3-service-restart（LINE 指令）'
    Write-Host '     或這個資料夾的 start / stop / restart / status（兩個一起）、uninstall（取消註冊）'
    Write-Host ''
    Write-Host '     ⚠️ 服務在背景跑，看不到黑色視窗，也不會跳出 --popup 的小提示。'
    Write-Host '        Telegram（經由 Notifier_TB）會在開機、關機、當掉、每次發送時各傳一則給你。'
}

function Invoke-Control([string]$verb) {
    # start / stop / restart。install.bat 開過權限給你的帳號，所以通常不用系統管理員；
    # 沒開過（舊版註冊的）就會被拒，這時才改用系統管理員再做一次。
    $nssm = Get-Nssm
    $zh = @{ start = '啟動'; stop = '停止'; restart = '重新啟動' }[$verb]
    if ($verb -ne 'start') { Write-Host '（停止時正在發送的那一則會等它送完，最多 25 秒）' }
    $failed = $false
    foreach ($key in $Keys) {
        $bot = $Bots[$key]
        if ($null -eq (Get-ServiceState $nssm $bot.Name)) {
            Write-Host "$($bot.Label)：還沒註冊成服務，跳過（要註冊請雙擊 service\install.bat）"
            continue
        }
        Write-Host "$($bot.Label)：正在$zh…"
        $result = Invoke-Nssm $nssm @($verb, $bot.Name)
        if ($result.Code -eq 0) { Write-Ok "$($bot.Label)：已$zh"; continue }
        if ((Test-Denied $result.Text) -and -not (Test-Admin)) {
            Write-Host '這個服務還沒開權限給你的帳號（較早的版本註冊的），要用系統管理員身分做一次。'
            Write-Host '（之後重新雙擊一次 install.bat，就不用每次都這樣了）'
            exit (Invoke-Elevated)
        }
        Write-Bad "$($bot.Label)：$zh失敗：$($result.Text)"
        $failed = $true
    }
    if ($verb -eq 'stop' -and $Keys -contains 'web') {
        Write-Host '     後台停著的時候，每週提醒不會自動發送。要讓它開機時不要再自己起來，請用 uninstall.bat。'
    }
    if ($failed) {
        Write-Host '   ・程式本來就開著（2-start／3-open-webhook 的黑色視窗）→ 先關掉那個視窗再試'
        Write-Host "   ・詳細訊息在 $Root\data\church_bot.log"
        Stop-Here 1
    }
}

function Show-Status {
    Write-Host '=== Windows 服務 ==='
    $zh = @{ 'SERVICE_RUNNING' = '執行中'; 'SERVICE_STOPPED' = '已停止'
             'SERVICE_START_PENDING' = '正在啟動'; 'SERVICE_STOP_PENDING' = '正在停止'
             'SERVICE_PAUSED' = '已暫停' }
    foreach ($key in $Keys) {
        $bot = $Bots[$key]
        $state = if (Test-Path $NssmPath) { Get-ServiceState $NssmPath $bot.Name } else { $null }
        if ($null -eq $state) {
            Write-Host "$($bot.Label)：還沒註冊成服務"
        } else {
            $known = $zh[$state]
            Write-Host ("$($bot.Label)（$($bot.Name)）：" + $(if ($known) { "$known（$state）" } else { $state }))
        }
    }
    if ($Keys -notcontains 'web') { return }
    Write-Host ''
    Write-Host '=== 各牧區 ==='
    if (-not (Test-Path $VenvPython)) {
        Write-Host '還沒安裝，請先雙擊 scripts\windows\1-install.bat'
        return
    }
    $env:PYTHONPATH = Join-Path $Root 'src'
    $env:PYTHONUTF8 = '1'
    Push-Location $Root
    try { & $VenvPython -m church_bot 狀態 } finally { Pop-Location }
}

function Uninstall-Bots {
    $nssm = Get-Nssm
    foreach ($key in $Keys) {
        $bot = $Bots[$key]
        if ($null -eq (Get-ServiceState $nssm $bot.Name)) { continue }
        Invoke-Nssm $nssm @('stop', $bot.Name) | Out-Null
        $result = Invoke-Nssm $nssm @('remove', $bot.Name, 'confirm')
        if ($result.Code -ne 0) { Write-Bad "移除失敗（$($bot.Name)）：$($result.Text)"; Stop-Here 1 }
        Write-Ok "已取消註冊：$($bot.Label)"
    }
}

# --------------------------------------------------------------------------- 開始

switch ($Action) {
    'status' { Show-Status }
    { $_ -in 'start', 'stop', 'restart' } { Invoke-Control $Action }
    'install' {
        # 註冊要系統管理員；建捷徑、開小圖示要用「你自己」的身分做（小圖示不該是系統管理員身分在跑）
        if (Test-Admin) { Install-Bots } else {
            $code = Invoke-Elevated
            if ($code -ne 0) { Write-Bad '註冊沒有完成，看上一個視窗的訊息。'; Stop-Here $code }
        }
        if (-not $Elevated) {
            Install-TrayShortcut
            Start-Tray
            Write-Ok '右下角小圖示：已經開好，以後登入 Windows 時會自己出現'
        }
    }
    'uninstall' {
        if (-not $Elevated) {
            $which = if ($Target -eq 'all') { '兩個服務（後台、LINE 指令）' } else { $Bots[$Target].Label }
            Write-Host "這會把 $which 從 Windows 移除。"
            Write-Host '設定、名單、發送紀錄都不會動，只是以後開機不會自己啟動，'
            Write-Host '要發提醒就得自己雙擊 scripts\windows\2-start.bat 並讓那個視窗開著。'
            Write-Host ''
            if ((Read-Host '確定要取消註冊嗎？(y/N)') -notmatch '^[Yy]') { Write-Host '取消，什麼都沒做。'; Stop-Here 0 }
        }
        if (Test-Admin) { Uninstall-Bots } else {
            $code = Invoke-Elevated
            if ($code -ne 0) { Stop-Here $code }
        }
        if (-not $Elevated -and $Target -eq 'all' -and (Test-Path $TrayShortcut)) {
            Remove-Item $TrayShortcut
            Write-Host '（登入時自動開小圖示的捷徑也拿掉了；雙擊 2-start／3-open-webhook 時小圖示照樣會出現）'
        }
        if (-not $Elevated) { Write-Ok '想恢復開機自動執行，雙擊 install.bat。' }
    }
}
Stop-Here 0

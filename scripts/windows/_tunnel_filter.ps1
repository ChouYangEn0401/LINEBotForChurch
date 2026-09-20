# Helper for 3-open-webhook.bat: reads cloudflared's log, and when the public URL appears,
# prints the Webhook URL clearly and copies it to the clipboard. Do not run directly.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$shown = $false
foreach ($line in $input) {
    if (-not $shown -and $line -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
        $shown = $true
        $webhook = $Matches[0] + '/line/webhook'
        try { Set-Clipboard -Value $webhook } catch { }
        Write-Host ''
        Write-Host '=============================================================='
        Write-Host ' 臨時網址已建立！Webhook URL（已自動複製，直接貼上即可）：'
        Write-Host ''
        Write-Host "   $webhook" -ForegroundColor Green
        Write-Host ''
        Write-Host ' 接下來：LINE Developers → Messaging API → Webhook URL 貼上'
        Write-Host ' → 按 Verify → 打開 Use webhook。'
        Write-Host ' 每次重開，網址都會變，要重新貼一次。'
        Write-Host '=============================================================='
        Write-Host ''
    }
    elseif ($line -match '\b(ERR|error)\b') {
        Write-Host $line -ForegroundColor Yellow
    }
}

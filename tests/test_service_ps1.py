"""scripts/windows/service/service.ps1：交給 nssm 的參數要原封不動到得了（不跳 UAC、不碰真的服務）。

2026-10-09：church-bot-webhook 註冊好了卻一直卡在 PAUSED。AppParameters 是
``--cloudflared "C:\\Program Files (x86)\\cloudflared\\cloudflared.exe"``，用 ``& $nssm @arguments`` 交給 nssm 時
Windows PowerShell 5.1 把引號弄丟，路徑被空白切成三段，程式一啟動就 exit 2、被 nssm 一直重開。
這裡把 service.ps1 裡的 Join-Arguments／Invoke-Nssm 拿出來，「nssm」換成一支把收到的參數印回來的 python。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "windows" / "service" / "service.ps1"

HARNESS = r"""
param([string]$Script, [string]$Py)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
$funcs = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                        $args[0].Name -in @('Join-Arguments', 'Invoke-Nssm') }, $true)
foreach ($f in $funcs) { Invoke-Expression $f.Extent.Text }
$cmd = 'import sys, json; sys.stdout.write(json.dumps(sys.argv[1:]))'
$r = Invoke-Nssm $Py @('-c', $cmd, 'set', 'church-bot-webhook', 'AppParameters',
    "-m church_bot tunnel --port 8787 --cloudflared `"C:\Program Files (x86)\cloudflared\cloudflared.exe`"",
    'pa"ss wo\"rd\', 'C:\Users\John Smith\')
$r.Text
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 才有這個問題")
def test_arguments_reach_nssm_exactly(tmp_path):
    harness = tmp_path / "harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8-sig")
    done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness),
                           "-Script", str(SCRIPT), "-Py", sys.executable],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert got == ["set", "church-bot-webhook", "AppParameters",
                   '-m church_bot tunnel --port 8787 --cloudflared "C:\\Program Files (x86)\\cloudflared\\cloudflared.exe"',
                   'pa"ss wo\\"rd\\', "C:\\Users\\John Smith\\"]

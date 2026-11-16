param([int]$Seconds = 0, [switch]$Record)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUTF8 = '1'
$botPython = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $botPython)) { throw '请先创建 Python 3.11+ 虚拟环境并安装 requirements.lock。' }
$botArgs = @('-u', 'main.py', 'run')
if ($Seconds -gt 0) { $botArgs += @('--seconds', "$Seconds") }
if ($Record) { $botArgs += '--record' }
& $botPython @botArgs
exit $LASTEXITCODE

# agent-sync 启动器（Windows）
# 用法: .\agent-sync.ps1 check | apply | list | doctor [agent...]
# 首次: python -m venv .venv; .venv\Scripts\pip install pyyaml
Set-Location $PSScriptRoot
$PY = ".venv\Scripts\python.exe"
if (-not (Test-Path $PY)) {
    $PY = "python"
}
& $PY "$PSScriptRoot\agent-sync" @args
exit $LASTEXITCODE

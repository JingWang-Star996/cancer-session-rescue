$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$buildPython = Join-Path $PSScriptRoot '.build-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    python -m venv .build-venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 with Tk is required.' }
}
& $buildPython -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }
& $buildPython -m PyInstaller --noconfirm --clean --onefile --windowed --name 'Codex 会话监控' --version-file version_info.txt --icon monitor.ico --add-data 'monitor.ico;.' --add-data 'optimization_guide.md;.' --add-data 'AGENT_API.md;.' --distpath dist --workpath build app.py
if ($LASTEXITCODE -ne 0) { throw 'Dashboard build failed.' }
& $buildPython -m PyInstaller --noconfirm --clean --onefile --console --name 'codex-monitor-agent' --version-file version_info.txt --distpath dist --workpath build-agent agent_api.py
if ($LASTEXITCODE -ne 0) { throw 'Agent CLI build failed.' }

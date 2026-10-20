$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $packageRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "Python environment missing: $python. See osworld_agent\README.md Windows setup."
}
Set-Location $projectRoot
if (-not $env:AGENTS_VIZ_PORT) { $env:AGENTS_VIZ_PORT = '8090' }
& $python -m osworld_agent.viz.server

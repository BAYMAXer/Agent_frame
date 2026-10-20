[CmdletBinding()]
param(
    [string]$Url = 'about:blank',
    [ValidateRange(1, 65535)]
    [int]$BrowserPort = 9222,
    [string]$ProfileDir = ''
)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $packageRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "Python environment missing: $python. See osworld_agent\README.md Windows setup."
}

$loginArgs = @('-m', 'osworld_agent.persistent_chrome', '--url', $Url,
               '--browser-port', $BrowserPort.ToString())
if (-not [string]::IsNullOrWhiteSpace($ProfileDir)) {
    $loginArgs += @('--browser-profile-dir', $ProfileDir)
}

Write-Host 'Opening the fixed AgentFrame task browser (separate from daily Chrome).'
Write-Host 'Sign in on that page. Return here and press Enter when finished.'
Write-Host 'Persistent cookies and site storage are kept; expired site sessions require signing in again.'
Push-Location $projectRoot
try {
    & $python @loginArgs
    if ($LASTEXITCODE -ne 0) {
        throw "The task browser could not complete login setup (exit $LASTEXITCODE)."
    }
}
finally {
    Pop-Location
}

$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$pidFile = Join-Path $projectRoot 'runs\document-lab.pid'
$expectedPython = [IO.Path]::GetFullPath((Join-Path $projectRoot '.venv\Scripts\python.exe'))

if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    Write-Host 'No Document Lab process started by start_document_lab.ps1 is recorded.'
    exit 0
}

$serverPid = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$serverPid)) {
    throw "Invalid process ID file: $pidFile"
}

$serverProcess = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
if (-not $serverProcess) {
    Remove-Item -LiteralPath $pidFile -Force
    Write-Host 'Document Lab is already stopped.'
    exit 0
}

if (-not $serverProcess.Path -or [IO.Path]::GetFullPath($serverProcess.Path) -ne $expectedPython) {
    throw "Recorded process $serverPid is not the project's virtual-environment Python. It was left running for safety."
}

$taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
& $taskkill /PID $serverPid /T /F | Out-Null
Remove-Item -LiteralPath $pidFile -Force
Write-Host 'Document Lab has stopped.'

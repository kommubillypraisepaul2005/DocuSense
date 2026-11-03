$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$siteUrl = 'http://127.0.0.1:8000/'
$healthUrl = 'http://127.0.0.1:8000/health'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logDir = Join-Path $projectRoot 'runs'
$stdoutLog = Join-Path $logDir 'document-lab.stdout.log'
$stderrLog = Join-Path $logDir 'document-lab.stderr.log'
$pidFile = Join-Path $logDir 'document-lab.pid'

Set-Location -LiteralPath $projectRoot

try {
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop | Select-Object -First 1
} catch {
    $listener = $null
}

if (-not $listener) {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Project Python environment not found at $python. Create .venv and install requirements.txt first."
    }

    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    $env:PYTHONIOENCODING = 'utf-8'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null

    $serverProcess = Start-Process -FilePath $python `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Set-Content -LiteralPath $pidFile -Value $serverProcess.Id -Encoding ascii

    $healthy = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
            if ($health.status -eq 'ok') {
                $healthy = $true
                break
            }
        } catch {
            # Keep polling while Uvicorn starts.
        }
    }

    if (-not $healthy) {
        if (-not $serverProcess.HasExited) { Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
        $details = if (Test-Path -LiteralPath $stderrLog) { (Get-Content -LiteralPath $stderrLog -Tail 30) -join [Environment]::NewLine } else { 'No server log was written.' }
        throw "Document Lab did not start. Server log: $stderrLog`n$details"
    }
} else {
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
    } catch {
        throw "Port 8000 is already occupied by process $($listener.OwningProcess), but its health endpoint is unavailable. The existing process was left unchanged."
    }
    if ($health.status -ne 'ok') {
        throw "Port 8000 is already occupied, but it did not identify itself as Document Lab. The existing process was left unchanged."
    }
}

Start-Process $siteUrl
Write-Host "Document Lab is ready at $siteUrl"

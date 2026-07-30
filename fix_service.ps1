$nssm = "C:\Users\tuanbm\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$python = "C:\Users\tuanbm\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$script = "C:\taihsmt\taihsmt.py"
$workDir = "C:\taihsmt"
$serviceName = "VNeGPDownloader"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $isAdmin) {
    Start-Process powershell "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

Write-Host "Configuring service parameters..."

& $nssm set $serviceName AppDirectory $workDir 2>&1
& $nssm set $serviceName AppThrottle 5000 2>&1
& $nssm set $serviceName AppExit Default Restart 2>&1
& $nssm set $serviceName AppStdout "$workDir\vnegp_cache\service_stdout.log" 2>&1
& $nssm set $serviceName AppStderr "$workDir\vnegp_cache\service_stderr.log" 2>&1
& $nssm set $serviceName AppEnvironmentExtra "PYTHONUNBUFFERED=1" 2>&1
& $nssm set $serviceName DisplayName "VNeGP Downloader Service" 2>&1

Write-Host "Starting service..."
& $nssm start $serviceName 2>&1
Start-Sleep -Seconds 3

$svc = Get-Service $serviceName
Write-Host "Service status: $($svc.Status)"

if ($svc.Status -eq 'Running') {
    Write-Host "SUCCESS - Server running on http://localhost:8000"
    Write-Host "Test: curl http://localhost:8000/api/status"
} else {
    Write-Host "FAILED - Check logs:"
    Write-Host "  stdout: $workDir\vnegp_cache\service_stdout.log"
    Write-Host "  stderr: $workDir\vnegp_cache\service_stderr.log"
    if (Test-Path "$workDir\vnegp_cache\service_stderr.log") {
        Write-Host "=== STDERR ==="
        Get-Content "$workDir\vnegp_cache\service_stderr.log"
    }
}

Start-Sleep -Seconds 10

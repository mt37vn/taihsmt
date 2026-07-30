param([switch]$Uninstall)

$nssm = "C:\Users\tuanbm\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$python = "C:\Users\tuanbm\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$script = "C:\taihsmt\taihsmt.py"
$workDir = "C:\taihsmt"
$serviceName = "VNeGPDownloader"

# Check admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")

if (-not $isAdmin -and (-not $Uninstall)) {
    Write-Host "Re-launching as Administrator..."
    Start-Process powershell "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall:`$$Uninstall" -Verb RunAs -Wait
    exit
}

if (-not $isAdmin -and $Uninstall) {
    Write-Error "Must run as Administrator to uninstall!"
    exit 1
}

if ($Uninstall) {
    Write-Host "Stopping and removing service..."
    & $nssm stop $serviceName 2>$null
    & $nssm remove $serviceName confirm
    Write-Host "Service removed."
    exit 0
}

Write-Host "=== Installing VNeGP Downloader Service ==="

& $nssm stop $serviceName 2>$null
& $nssm remove $serviceName confirm 2>$null

& $nssm install $serviceName $python $script
if ($LASTEXITCODE -ne 0) { Write-Error "Install failed"; exit 1 }

& $nssm set $serviceName DisplayName "VNeGP Downloader Service"
& $nssm set $serviceName AppDirectory $workDir
& $nssm set $serviceName Start SERVICE_AUTO_START
& $nssm set $serviceName AppThrottle 5000
& $nssm set $serviceName AppExit Default Restart
& $nssm set $serviceName AppStdout "$workDir\vnegp_cache\service_stdout.log"
& $nssm set $serviceName AppStderr "$workDir\vnegp_cache\service_stderr.log"
& $nssm set $serviceName AppEnvironmentExtra "PYTHONUNBUFFERED=1"

Write-Host "Starting service..."
& $nssm start $serviceName

Start-Sleep -Seconds 2
& $nssm status $serviceName

Write-Host ""
Write-Host "=== Done ==="
Write-Host "Service 'VNeGPDownloader' running on http://localhost:8000"

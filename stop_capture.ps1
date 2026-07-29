param(
    [string]$OutputDir = "C:\taihsmt\vnegp_cache"
)

$pidFile = Join-Path $OutputDir "_capture_pid.txt"

if (-not (Test-Path $pidFile)) {
    Write-Error "No capture PID file found at $pidFile. Is capture running?"
    exit 1
}

$pid = [int][System.IO.File]::ReadAllText($pidFile)
Write-Host "Stopping capture process PID: $pid"

try {
    $proc = Get-Process -Id $pid -ErrorAction Stop
    $proc.Kill()
    Write-Host "Capture process killed."
} catch {
    Write-Warning "Process $pid not found (already stopped)."
}

# Clean up marker files
$startMarker = Join-Path $OutputDir "_capture_start.txt"
foreach ($f in @($pidFile, $startMarker)) {
    if (Test-Path $f) {
        Remove-Item $f -Force
    }
}

# List output captures
$captures = Get-ChildItem -Path $OutputDir -Filter "capture_*.pcapng" | Sort-Object LastWriteTime -Descending
if ($captures) {
    Write-Host ""
    Write-Host "Recent captures:"
    $captures | Select-Object -First 5 | Format-Table Name, Length, LastWriteTime
}

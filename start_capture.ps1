param(
    [string]$OutputDir = "C:\taihsmt\vnegp_cache",
    [int]$Duration = 120
)

$ErrorActionPreference = "Stop"

$tshark = "C:\Program Files\Wireshark\tshark.exe"
if (-not (Test-Path $tshark)) {
    Write-Error "tshark not found at $tshark"
    exit 1
}

# Find loopback interface
$ifaces = & $tshark -D 2>&1
$loopback = $ifaces | Select-String -Pattern "Loopback|NPF_Loopback" | Select-Object -First 1
if (-not $loopback) {
    Write-Error "No loopback interface found. Is Npcap installed with loopback support?"
    exit 1
}
$ifaceNum = $loopback.ToString().Split('.')[0]
$ifaceName = $loopback.ToString().Trim()

Write-Host "=== Loopback Capture ==="
Write-Host "Interface: $ifaceName"
Write-Host "Filter: tcp port 1234"
Write-Host "Duration: ${Duration}s"
Write-Host "Output: $OutputDir"
Write-Host ""

$timestamp = (Get-Date -Format "yyyyMMdd_HHmmss")
$outFile = Join-Path $OutputDir "capture_$timestamp.pcapng"
$pidFile = Join-Path $OutputDir "_capture_pid.txt"
$startMarker = Join-Path $OutputDir "_capture_start.txt"

# Write start timestamp
[System.IO.File]::WriteAllText($startMarker, "$([DateTime]::UtcNow.ToString('o'))")

# Write PID for stop script
[System.IO.File]::WriteAllText($pidFile, "$PID")

Write-Host "Starting capture -> $outFile"
Write-Host "PID: $PID"
Write-Host "Stop with: .\stop_capture.ps1 (or press Ctrl+C)"
Write-Host ""

$proc = Start-Process -FilePath $tshark -ArgumentList @(
    "-i", "$ifaceNum",
    "-f", "tcp port 1234",
    "-w", $outFile,
    "-b", "duration:60"
) -NoNewWindow -PassThru

Write-Host "Capture running (PID $($proc.Id)). Waiting up to ${Duration}s..."
$proc.WaitForExit($Duration * 1000)

if (-not $proc.HasExited) {
    Write-Host "Duration reached. Stopping capture..."
    $proc.Kill()
}
Write-Host "Capture finished."

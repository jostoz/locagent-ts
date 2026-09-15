param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string[]]$PythonArgs,
    [int]$FreeMiBFloor = 1024,
    [int]$IntervalSeconds = 10,
    [Parameter(Mandatory = $true)][string]$LogDir
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMddTHHmmssZ'
$watchdogLog = Join-Path $LogDir "gpu-watchdog-$stamp.log"
$stdoutLog = Join-Path $LogDir "executor-$stamp.stdout.log"
$stderrLog = Join-Path $LogDir "executor-$stamp.stderr.log"

function Get-FreeMiB {
    $rows = & nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $rows) { return $null }
    return [int](($rows | ForEach-Object { [int]$_.Trim() } | Measure-Object -Minimum).Minimum)
}

$before = Get-FreeMiB
"$((Get-Date).ToUniversalTime().ToString('o')) before_free_mib=$before floor_mib=$FreeMiBFloor" | Set-Content -LiteralPath $watchdogLog
$proc = Start-Process -FilePath $Python -ArgumentList $PythonArgs -PassThru -NoNewWindow `
    -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
"$((Get-Date).ToUniversalTime().ToString('o')) target_pid=$($proc.Id) stdout=$stdoutLog stderr=$stderrLog" | Add-Content -LiteralPath $watchdogLog

$intervened = $false
while (-not $proc.HasExited) {
    Start-Sleep -Seconds $IntervalSeconds
    $proc.Refresh()
    if ($proc.HasExited) { break }
    $free = Get-FreeMiB
    "$((Get-Date).ToUniversalTime().ToString('o')) target_pid=$($proc.Id) free_mib=$free" | Add-Content -LiteralPath $watchdogLog
    if ($null -ne $free -and $free -lt $FreeMiBFloor) {
        Stop-Process -Id $proc.Id -Force
        "$((Get-Date).ToUniversalTime().ToString('o')) intervention=free_memory_floor target_pid=$($proc.Id)" | Add-Content -LiteralPath $watchdogLog
        $intervened = $true
        break
    }
}
$proc.WaitForExit()
$proc.Refresh()
$targetExit = $proc.ExitCode
"$((Get-Date).ToUniversalTime().ToString('o')) target_exit=$targetExit intervention=$intervened" | Add-Content -LiteralPath $watchdogLog
Get-Content -LiteralPath $watchdogLog
exit $targetExit

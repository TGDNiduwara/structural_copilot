# wait_for_backlog_done.ps1
# Detached watcher: polls for LIVE_BACKLOG_DONE (which implies CHAIN_DONE)
# and writes READY_FOR_APP_TEST when both markers exist. Reads only - never
# touches Robot. Exits 0 on ready, 1 on 12h timeout.
$val = "C:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot\batch\live_val_results"
$deadline = (Get-Date).AddHours(12)
while ((Get-Date) -lt $deadline) {
    $c = Test-Path (Join-Path $val "CHAIN_DONE")
    $b = Test-Path (Join-Path $val "LIVE_BACKLOG_DONE")
    if ($c -and $b) {
        Add-Content -Path (Join-Path $val "READY_FOR_APP_TEST") `
            -Value ("ready {0}" -f (Get-Date -Format "HH:mm:ss"))
        Write-Output "READY_FOR_APP_TEST"
        exit 0
    }
    Start-Sleep -Seconds 45
}
Write-Output "TIMEOUT waiting for CHAIN_DONE/LIVE_BACKLOG_DONE"
exit 1

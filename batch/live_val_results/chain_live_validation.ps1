# chain_live_validation.ps1
# =========================
# Single long-running driver for the live surrogate validation.
# Waits for the ALREADY-RUNNING grid stage (batch/validate_surrogate_live.py
# grid, started separately) to finish, then runs the remaining stages in
# order and writes CHAIN_DONE when everything is complete.
#
# Progress is appended to chain_progress.txt; check that file (or the
# per-stage .json files) - do NOT fire repeated short poll commands.

$ErrorActionPreference = "Continue"
$root = "C:\Users\dinat\Downloads\structural_multi_app_agent\structural_copilot"
$py   = Join-Path $root "venv\Scripts\python.exe"
$val  = Join-Path $root "batch\live_val_results"
Set-Location $root

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Output $line
    Add-Content -Path (Join-Path $val "chain_progress.txt") -Value $line
}

function Wait-StageJson($stage, $timeoutMin) {
    $deadline = (Get-Date).AddMinutes($timeoutMin)
    while (-not (Test-Path (Join-Path $val "$stage.json"))) {
        if (Test-Path (Join-Path $val "$stage.ERROR.txt")) {
            Log "STAGE $stage FAILED (see $stage.ERROR.txt)"
            return $false
        }
        if ((Get-Date) -gt $deadline) {
            Log "STAGE $stage TIMEOUT after $timeoutMin min"
            return $false
        }
        Start-Sleep -Seconds 60
    }
    return $true
}

function Run-Stage($stage) {
    Log "starting stage $stage"
    $p = Start-Process -FilePath $py `
        -ArgumentList "-u", "batch\validate_surrogate_live.py", $stage `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $val "$stage`_out.txt") `
        -RedirectStandardError  (Join-Path $val "$stage`_err.txt") `
        -PassThru -WindowStyle Hidden
    $ok = Wait-StageJson $stage 240
    if ($ok) {
        Log "stage $stage DONE"
    } else {
        Log "stage $stage NOT OK - chain aborting"
    }
    return $ok
}

Log "chain started; waiting for the running grid stage to complete..."
if (Wait-StageJson "grid" 300) {
    Log "grid DONE (was launched separately)"
} else {
    Log "grid failed - chain aborting"
    exit 1
}

$stages = @("surrogate", "resume", "reconnect", "crossrun", "ehvi")
foreach ($s in $stages) {
    $ok = Run-Stage $s
    if (-not $ok) { exit 1 }
}

Log "all stages done; running report"
& $py "batch\validate_surrogate_live.py" "report" *>> (Join-Path $val "report_out.txt")
Log "CHAIN_DONE"
Add-Content -Path (Join-Path $val "CHAIN_DONE") -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss")

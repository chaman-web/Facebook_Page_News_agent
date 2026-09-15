$ErrorActionPreference = "Stop"

$projectDir = "C:\Users\akmal\OneDrive\Desktop\facebook news agent"
$logsDir    = "$projectDir\logs"

# Create logs folder if not exists
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir }

# --- FIX 1: Enable wake timers (AC and DC) ---
Write-Host "Enabling wake timers (AC and DC)..."
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setactive SCHEME_CURRENT
Write-Host "OK Wake timers enabled"

# --- FIX 2: Set DC (battery) sleep to 30 min so machine sleeps lightly ---
# Prevents deep hibernate on battery which blocks wake timers
Write-Host "Setting battery sleep timeout to 30 minutes..."
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 1800
powercfg /setactive SCHEME_CURRENT
Write-Host "OK Battery sleep timeout set"


# --- Job 1: Fetch & Build - every 4 hours (PKT times) ---
$action1 = New-ScheduledTaskAction `
    -Execute (Join-Path $projectDir "run_job1_fetch.bat") `
    -WorkingDirectory $projectDir

$trigger1 = @(
    $(New-ScheduledTaskTrigger -Daily -At "00:00"),
    $(New-ScheduledTaskTrigger -Daily -At "04:00"),
    $(New-ScheduledTaskTrigger -Daily -At "08:00"),
    $(New-ScheduledTaskTrigger -Daily -At "12:00"),
    $(New-ScheduledTaskTrigger -Daily -At "16:00"),
    $(New-ScheduledTaskTrigger -Daily -At "20:00")
)

$settings1 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -WakeToRun

# FIX 6: Restart up to 3 times on crash, 5 min apart
$settings1.RestartCount    = 3
$settings1.RestartInterval = "PT5M"

Register-ScheduledTask `
    -TaskName "GlobalPulseNews_Job1_Fetch" `
    -Action   $action1 `
    -Trigger  $trigger1 `
    -Settings $settings1 `
    -RunLevel Highest `
    -Force `
    -ErrorAction Stop

Write-Host "OK Job 1 registered - every 4 hours"

# --- Job 2: Publish - enabled only while verified queue work exists ---
$action2 = New-ScheduledTaskAction `
    -Execute (Join-Path $projectDir "run_job2_publish.bat") `
    -WorkingDirectory $projectDir

$trigger2 = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings2 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -WakeToRun

# FIX 6: Restart up to 3 times on crash, 5 min apart
$settings2.RestartCount    = 3
$settings2.RestartInterval = "PT5M"

Register-ScheduledTask `
    -TaskName "GlobalPulseNews_Job2_Publish" `
    -Action   $action2 `
    -Trigger  $trigger2 `
    -Settings $settings2 `
    -RunLevel Highest `
    -Force `
    -ErrorAction Stop

Write-Host "OK Job 2 registered - self-disables while verified queue is empty"

# Remove the temporary per-user task used when the existing elevated task
# could not be updated without an administrator session.
Unregister-ScheduledTask `
    -TaskName "GlobalPulseNews_Job2_Publish_10Min" `
    -Confirm:$false `
    -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. Open taskschd.msc to verify."

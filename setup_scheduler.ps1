$ErrorActionPreference = "Stop"

$projectDir = "C:\Users\akmal\OneDrive\Desktop\facebook news agent"
$logsDir    = "$projectDir\logs"

# Create logs folder if not exists
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir }

# --- Job 1: Fetch & Build — every 4 hours (PKT times) ---
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
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName "GlobalPulseNews_Job1_Fetch" `
    -Action   $action1 `
    -Trigger  $trigger1 `
    -Settings $settings1 `
    -RunLevel Highest `
    -Force `
    -ErrorAction Stop

Write-Host "OK Job 1 registered - every 4 hours"

# --- Job 2: Publish — enabled only while verified queue work exists ---
$action2 = New-ScheduledTaskAction `
    -Execute (Join-Path $projectDir "run_job2_publish.bat") `
    -WorkingDirectory $projectDir

# This trigger is a recovery fallback while the task is enabled. The task
# disables itself as soon as both verified tiers are empty. Job 1 re-enables
# and starts it only after verified work enters the queue.
$trigger2 = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings2 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

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

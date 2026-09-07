$projectDir = "C:\Users\akmal\OneDrive\Desktop\facebook news agent"
$python     = "$projectDir\.venv\Scripts\python.exe"
$logsDir    = "$projectDir\logs"

# Create logs folder if not exists
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir }

# --- Job 1: Fetch & Build — every 3 hours (PKT times) ---
$action1 = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "agent.py --fetch --image" `
    -WorkingDirectory $projectDir

$trigger1 = @(
    $(New-ScheduledTaskTrigger -Daily -At "06:00"),
    $(New-ScheduledTaskTrigger -Daily -At "09:00"),
    $(New-ScheduledTaskTrigger -Daily -At "12:00"),
    $(New-ScheduledTaskTrigger -Daily -At "15:00"),
    $(New-ScheduledTaskTrigger -Daily -At "18:00"),
    $(New-ScheduledTaskTrigger -Daily -At "21:00"),
    $(New-ScheduledTaskTrigger -Daily -At "00:00"),
    $(New-ScheduledTaskTrigger -Daily -At "03:00")
)

$settings1 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

Register-ScheduledTask `
    -TaskName "GlobalPulseNews_Job1_Fetch" `
    -Action   $action1 `
    -Trigger  $trigger1 `
    -Settings $settings1 `
    -RunLevel Highest `
    -Force

Write-Host "OK Job 1 registered - every 3 hours"

# --- Job 2: Publish — 13:00, 18:00, 00:00 PKT (= 08:00, 13:00, 19:00 UTC) ---
$action2 = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "agent.py --publish" `
    -WorkingDirectory $projectDir

$trigger2 = @(
    $(New-ScheduledTaskTrigger -Daily -At "13:00"),
    $(New-ScheduledTaskTrigger -Daily -At "18:00"),
    $(New-ScheduledTaskTrigger -Daily -At "00:00")
)

$settings2 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

Register-ScheduledTask `
    -TaskName "GlobalPulseNews_Job2_Publish" `
    -Action   $action2 `
    -Trigger  $trigger2 `
    -Settings $settings2 `
    -RunLevel Highest `
    -Force

Write-Host "OK Job 2 registered - 13:00 / 18:00 / 00:00 PKT"
Write-Host ""
Write-Host "Done. Open taskschd.msc to verify."

# Registers a Windows Scheduled Task that runs alerts.py every 15 minutes,
# 09:00-16:00, Monday-Friday (Asian market hours, local time).
# Run this ONCE, after you've tested the Teams webhook (python alerts.py --test).

$ErrorActionPreference = "Stop"
$py  = (Get-Command python).Source
$dir = "C:\Users\jeffl\Test"

$action = New-ScheduledTaskAction -Execute $py -Argument "alerts.py" -WorkingDirectory $dir

# Weekdays at 09:00, then repeat every 15 min for 7 hours (until 16:00)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:00am
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 9:00am `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Hours 7)).Repetition

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "CoverageMonitorAlerts" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Asian coverage outsized-move alerts to Teams" -Force | Out-Null

Write-Output "Registered 'CoverageMonitorAlerts' (every 15 min, 09:00-16:00, Mon-Fri)."
Write-Output "Remove later with:  Unregister-ScheduledTask -TaskName CoverageMonitorAlerts -Confirm:`$false"

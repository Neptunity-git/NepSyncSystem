# NepSync を Windows のタスクスケジューラに登録する（つなぎ。IdeaPad の VM に移したら、このタスクと scripts\ を消す）
# 使い方：PowerShell で、このフォルダの一つ上（NepSyncSystem）から  powershell -ExecutionPolicy Bypass -File scripts\install-run.ps1
# - 「NepSync\run」：ログインしたときと、5分ごとの見回りで scripts\run.cmd を起こす（動いているあいだは新しく起動しない）
# 管理者の権限は要らない（ログインしているあいだだけ動く。Neplector と同じ）

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$action = New-ScheduledTaskAction -Execute (Join-Path $root "scripts\run.cmd")
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskPath "\NepSync\" -TaskName "run" -Action $action -Trigger $atLogon, $every5 -Settings $settings `
    -Principal $principal -Force `
    -Description "NepSync：同期を動かし続ける（5分ごとに見回り、止まっていたら起こす）。IdeaPad の VM に移したら消す" | Out-Null
Write-Output "タスク NepSync\run を登録しました"
Start-ScheduledTask -TaskPath "\NepSync\" -TaskName "run"
Write-Output "起動しました。ログ：data\logs\run.log"

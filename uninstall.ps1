# Screen Stocks Bot uninstaller: stops the bot and removes the shortcuts. Your data folder is left alone.
$dir = $PSScriptRoot
$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
& $wscript (Join-Path $dir "run_watchdog.vbs") --port 8641 --stop
$paths = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Screen Stocks Bot.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Stop Screen Stocks Bot.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "Screen Stocks Bot.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Screen Stocks Bot")
)
foreach ($p in $paths) { if (Test-Path $p) { Remove-Item $p -Recurse -Force; Write-Host "Removed $p" } }
Write-Host "Done. Delete this folder to remove the bot completely (data\ holds your settings and history)."

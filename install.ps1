<#
  Screen Stocks Bot installer. Run Install.bat (it calls this).
  - makes sure Python 3.10+ is installed (offers to install it with winget)
  - installs Pillow (used for screenshots when an automatic go-public try fails; optional)
  - copies the recommended settings into data\config.json the first time
  - adds "Screen Stocks Bot" and "Stop Screen Stocks Bot" shortcuts to the Desktop and Start menu
  - optionally starts the bot when you log on
  - starts the bot and opens the dashboard
  Nothing here needs admin rights.
#>
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$port = 8641
function Say($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }
function Ask($q) { (Read-Host "$q [y/n]") -match '^(y|yes)$' }

Say "Screen Stocks Bot setup" Cyan
Say "Folder: $dir"

# 1. Python
function Find-Python {
    foreach ($c in @(@("py", "-3"), @("python"))) {
        try {
            $v = & $c[0] $c[1..9] -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -and [version]$v -ge [version]"3.10") { return ,$c }
        } catch {}
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Say "Python 3.10 or newer was not found." Yellow
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Install Python 3.12 now with winget?")) {
        winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        $py = Find-Python
    }
    if (-not $py) {
        Say "Install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run Install.bat again." Red
        exit 1
    }
}
Say ("Python: " + (& $py[0] $py[1..9] --version)) Green

# 2. Pillow (optional)
& $py[0] $py[1..9] -m pip install --user --quiet --disable-pip-version-check pillow 2>$null
if ($LASTEXITCODE -eq 0) { Say "Pillow: ok" Green } else { Say "Pillow could not be installed (optional, the bot runs without it)." Yellow }

# 3. Settings
$data = Join-Path $dir "data"
New-Item -ItemType Directory -Force $data | Out-Null
$cfg = Join-Path $data "config.json"
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $dir "recommended-config.json") $cfg
    Say "Settings: recommended settings copied to data\config.json" Green
} else {
    Say "Settings: kept your existing data\config.json" Green
}

# 4. Game mod support
$mods = Join-Path $env:USERPROFILE "AppData\LocalLow\Conradical Games\Screen Stocks\mods"
if (Test-Path (Join-Path $mods "export")) {
    Say "Game: mod export found" Green
} else {
    Say "Game: no mod export yet. In Screen Stocks open Settings and turn on Mod Support, then keep the game running." Yellow
}

# 5. Shortcuts
$vbs = Join-Path $dir "run_watchdog.vbs"
$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
$icon = Join-Path $dir "assets\icon.ico"
$shell = New-Object -ComObject WScript.Shell
function Make-Link($path, $arguments, $desc, $ico) {
    $l = $shell.CreateShortcut($path)
    $l.TargetPath = $wscript; $l.Arguments = $arguments; $l.WorkingDirectory = $dir
    $l.IconLocation = $ico; $l.Description = $desc; $l.Save()
}
$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Screen Stocks Bot"
New-Item -ItemType Directory -Force $startMenu | Out-Null
foreach ($f in @([Environment]::GetFolderPath("Desktop"), $startMenu)) {
    Make-Link (Join-Path $f "Screen Stocks Bot.lnk") "`"$vbs`" --port $port --open --" "Start the Screen Stocks Bot and open its dashboard" "$icon,0"
    Make-Link (Join-Path $f "Stop Screen Stocks Bot.lnk") "`"$vbs`" --port $port --stop" "Stop the Screen Stocks Bot" "$env:SystemRoot\System32\shell32.dll,27"
}
Say "Shortcuts: Desktop and Start menu" Green

# 6. Start with Windows (optional)
$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "Screen Stocks Bot.lnk"
if (Ask "Start the bot automatically when you log on?") {
    Make-Link $startup "`"$vbs`" --port $port --" "Start the Screen Stocks Bot in the background" "$icon,0"
    Say "Autostart: on" Green
} elseif (Test-Path $startup) {
    Remove-Item $startup
    Say "Autostart: off" Green
}

# 7. Start it
& $wscript $vbs --port $port --open --
Say ""
Say "Done. The dashboard opens at http://127.0.0.1:$port" Cyan
Say "The bot starts DISARMED: it watches but does not trade until you press Arm on the dashboard." Cyan


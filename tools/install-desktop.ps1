<#
.SYNOPSIS
  Installs openFTTH as a desktop app for the current Windows user, from this checkout.

.DESCRIPTION
  Builds the release app if needed, checks the free prerequisites (Node 23.6 or newer, the
  engine's Python environment), and adds openFTTH to the Start menu and the desktop. The app
  starts its own harness and design engine when opened; nothing runs in the background
  otherwise. Everything it uses is free: no account or key is needed to design with
  "Design automatically". A local model through Ollama is an optional, free crew engine.

  Run again after pulling new code to rebuild. -Uninstall removes the shortcuts.
#>
param([switch]$Uninstall, [switch]$NoDesktopShortcut)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $repo "desktop\target\release\openftth.exe"
$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "openFTTH.lnk"
$desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "openFTTH.lnk"

if ($Uninstall) {
    foreach ($l in @($startMenu, $desktop)) { if (Test-Path $l) { Remove-Item $l; "removed $l" } }
    return
}

function Need($ok, $what, $how) {
    if (-not $ok) { throw "$what is missing. $how" }
    "ok  $what"
}

# Node 23.6+ runs the harness's TypeScript directly.
$node = (Get-Command node -ErrorAction SilentlyContinue).Source
$nodeOk = $false
if ($node) { $v = (& $node --version).TrimStart("v").Split("."); $nodeOk = ([int]$v[0] -gt 23) -or ([int]$v[0] -eq 23 -and [int]$v[1] -ge 6) }
Need $nodeOk "Node 23.6 or newer on PATH" "Install the current Node.js (free) from nodejs.org."

# The engine's own environment, made once with `uv sync` (uv is free).
$engineExe = Join-Path $repo "engine\.venv\Scripts\openftth-engine.exe"
if (-not (Test-Path $engineExe)) {
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { $uv = Join-Path $HOME ".local\bin\uv.exe" }
    Need (Test-Path $uv) "uv" "Install uv (free): https://docs.astral.sh/uv/"
    Push-Location (Join-Path $repo "engine"); & $uv sync; Pop-Location
}
Need (Test-Path $engineExe) "design engine environment" "Run 'uv sync' in the engine folder."

# Harness dependencies.
if (-not (Test-Path (Join-Path $repo "harness\node_modules"))) {
    Push-Location (Join-Path $repo "harness"); corepack pnpm install --frozen-lockfile; Pop-Location
}
"ok  harness dependencies"

# The app itself.
$src = Get-ChildItem (Join-Path $repo "desktop\crates") -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not (Test-Path $exe) -or (Get-Item $exe).LastWriteTime -lt $src.LastWriteTime) {
    Push-Location (Join-Path $repo "desktop"); cargo build --release -p openftth-app; Pop-Location
}
Need (Test-Path $exe) "release app" "Install Rust (free) from rustup.rs and run again."

$shell = New-Object -ComObject WScript.Shell
$targets = @($startMenu)
if (-not $NoDesktopShortcut) { $targets += $desktop }
foreach ($l in $targets) {
    $s = $shell.CreateShortcut($l)
    $s.TargetPath = $exe
    $s.WorkingDirectory = Split-Path $exe
    $s.IconLocation = "$exe,0"
    $s.Description = "openFTTH: fibre access design with a crew"
    $s.Save()
    "added $l"
}

$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollama) { "free local crew engine available: Ollama (pick it in the composer's engine menu)" }
else { "optional: install Ollama (free) to run the crew on this PC: https://ollama.com" }
"done. Open openFTTH from the Start menu or the desktop."

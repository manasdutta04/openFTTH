<#
.SYNOPSIS
  Stages openFTTH as a self-contained Windows app folder (and, with Inno Setup, the installer).

.DESCRIPTION
  The staged folder needs nothing installed on the user's PC: it carries the desktop app, the
  bundled harness, its own Node and its own Python with the design engine in it, plus the
  rulebooks, operator packs, crew and the Gent sample. The folder mirrors the repository's
  layout, so the harness and the engine find their resources the same way they do in a checkout.

    dist\openFTTH\
      openftth.exe
      node\node.exe
      harness\dist\*.js
      engine\python\python.exe   (python -m openftth_engine)
      rulebooks\ operators\ crews\ profiles\ fixtures\gent\
      LICENSE NOTICE README.md version.txt

  Needs on the build machine: Rust, Node 24 + pnpm, uv (all free). Inno Setup 6 (free) only for
  the installer; without it the staged folder is still complete and runnable.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\package-windows.ps1
  powershell -File tools\package-windows.ps1 -Installer
#>
param(
    [switch]$Installer,
    [switch]$SkipBuild,
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Out) { $Out = Join-Path $repo "dist" }
$stage = Join-Path $Out "openFTTH"

function Step($what) { Write-Host "== $what" }
function Run($exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "$exe $($argv -join ' ') failed ($LASTEXITCODE)" }
}

# One version for everything: the desktop workspace's.
$version = (Select-String -Path (Join-Path $repo "desktop\Cargo.toml") -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
Step "openFTTH $version -> $stage"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null

# ---- the desktop app ---------------------------------------------------------------------------
if (-not $SkipBuild) {
    Step "desktop app (release)"
    Push-Location (Join-Path $repo "desktop")
    try { Run "cargo" @("build", "--release", "-p", "openftth-app") } finally { Pop-Location }
}
Copy-Item (Join-Path $repo "desktop\target\release\openftth.exe") $stage

# ---- the harness, bundled, and the Node that runs it ---------------------------------------------
if (-not $SkipBuild) {
    Step "harness bundle"
    Push-Location (Join-Path $repo "harness")
    try {
        Run "pnpm" @("install", "--frozen-lockfile")
        Run "pnpm" @("build")
    } finally { Pop-Location }
}
New-Item -ItemType Directory -Force (Join-Path $stage "harness\dist") | Out-Null
Get-ChildItem (Join-Path $repo "harness\dist") -Filter *.js | Copy-Item -Destination (Join-Path $stage "harness\dist")

Step "node runtime"
$node = (Get-Command node).Source
$nodeVersion = (& $node --version).TrimStart("v")
if ([int]$nodeVersion.Split(".")[0] -lt 24) { throw "Node 24 or newer is needed to bundle (found $nodeVersion)" }
New-Item -ItemType Directory -Force (Join-Path $stage "node") | Out-Null
Copy-Item $node (Join-Path $stage "node\node.exe")
$nodeLicense = Join-Path (Split-Path $node) "LICENSE"
if (Test-Path $nodeLicense) { Copy-Item $nodeLicense (Join-Path $stage "node\LICENSE") }

# ---- Python with the engine in it ----------------------------------------------------------------
Step "python runtime + engine"
$py = (& uv python find 3.12).Trim()
if (-not $py -or -not (Test-Path $py)) {
    Run "uv" @("python", "install", "3.12")
    $py = (& uv python find 3.12).Trim()
}
$pyHome = Split-Path $py
$pyStage = Join-Path $stage "engine\python"
Copy-Item -Recurse $pyHome $pyStage
# A copied interpreter is ours to fill: drop the "managed elsewhere" marker uv puts on its Pythons.
Get-ChildItem $pyStage -Recurse -Filter "EXTERNALLY-MANAGED" | Remove-Item -Force
$req = Join-Path $Out "engine-requirements.txt"
Push-Location (Join-Path $repo "engine")
try {
    Run "uv" @("export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project", "-o", $req)
} finally { Pop-Location }
$pyExe = Join-Path $pyStage "python.exe"
Run "uv" @("pip", "install", "--python", $pyExe, "--no-cache", "-r", $req)
Run "uv" @("pip", "install", "--python", $pyExe, "--no-cache", "--no-deps", (Join-Path $repo "engine"))
# Bytecode caches and test suites are not needed at run time.
Get-ChildItem $pyStage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem (Join-Path $pyStage "Lib\site-packages") -Recurse -Directory |
    Where-Object { $_.Name -in @("tests", "test") -and $_.FullName -notmatch "openftth_engine" } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ---- resources, laid out as in the repository --------------------------------------------------
Step "resources"
foreach ($d in @("rulebooks", "operators", "profiles", "exports")) {
    Copy-Item -Recurse (Join-Path $repo $d) (Join-Path $stage $d)
}
New-Item -ItemType Directory -Force (Join-Path $stage "crews") | Out-Null
Copy-Item -Recurse (Join-Path $repo "crews\openftth") (Join-Path $stage "crews\openftth")
New-Item -ItemType Directory -Force (Join-Path $stage "fixtures\gent") | Out-Null
Get-ChildItem (Join-Path $repo "fixtures\gent") -File | Copy-Item -Destination (Join-Path $stage "fixtures\gent")
foreach ($f in @("LICENSE", "NOTICE", "README.md")) { Copy-Item (Join-Path $repo $f) $stage }
Set-Content -Path (Join-Path $stage "version.txt") -Value $version -Encoding ascii

# ---- smoke: the staged folder works on its own -------------------------------------------------
Step "smoke: engine"
$smoke = Join-Path $Out "smoke"
if (Test-Path $smoke) { Remove-Item -Recurse -Force $smoke }
$env:PYTHONNOUSERSITE = "1"
Run $pyExe @("-m", "openftth_engine", "design",
    "--area", (Join-Path $stage "fixtures\gent\area-small.geojson"),
    "--osm", (Join-Path $stage "fixtures\gent\gent.osm"),
    "--rulebook", "generic-gpon",
    "--out", $smoke)
Step "smoke: harness"
$env:OPENFTTH_APP_TOKEN = "smoke"
$env:OPENFTTH_PORT = "0"
$env:OPENFTTH_DATA_DIR = Join-Path $smoke "data"
$p = Start-Process -FilePath (Join-Path $stage "node\node.exe") `
    -ArgumentList @("--no-warnings", "`"$(Join-Path $stage "harness\dist\index.js")`"") `
    -WorkingDirectory (Join-Path $stage "harness") -PassThru -NoNewWindow -RedirectStandardError (Join-Path $Out "smoke-harness.log")
try {
    $url = $null
    for ($i = 0; $i -lt 60 -and -not $url; $i++) {
        Start-Sleep -Milliseconds 250
        $m = Select-String -Path (Join-Path $Out "smoke-harness.log") -Pattern "listening on (http://127\.0\.0\.1:\d+)" -ErrorAction SilentlyContinue
        if ($m) { $url = $m.Matches[0].Groups[1].Value }
    }
    if (-not $url) { throw "the staged harness did not start; see $Out\smoke-harness.log" }
    $health = Invoke-RestMethod "$url/api/health"
    if (-not $health.ok) { throw "staged harness health: $($health | ConvertTo-Json -Compress)" }
    $books = Invoke-RestMethod "$url/api/rulebooks" -Headers @{ authorization = "Bearer smoke" }
    if (@($books).Count -lt 1) { throw "the staged harness finds no rulebooks" }
    "ok  harness at $url, $(@($books).Count) rulebooks"
} finally {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    Remove-Item Env:OPENFTTH_APP_TOKEN, Env:OPENFTTH_PORT, Env:OPENFTTH_DATA_DIR -ErrorAction SilentlyContinue
}
Remove-Item -Recurse -Force $smoke, $req -ErrorAction SilentlyContinue

$size = [math]::Round(((Get-ChildItem $stage -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB)
Step "staged $size MB"

# ---- the installer ---------------------------------------------------------------------------
if ($Installer) {
    $iscc = @(
        (Get-Command iscc -ErrorAction SilentlyContinue).Source,
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 (free) is needed for -Installer: https://jrsoftware.org/isinfo.php" }
    Step "installer"
    Run $iscc @("/Qp", "/DAppVersion=$version", "/DStage=$stage", "/O$Out", (Join-Path $repo "tools\installer\openftth.iss"))
    $setup = Join-Path $Out "openFTTH-Setup-$version.exe"
    $hash = (Get-FileHash -Algorithm SHA256 $setup).Hash.ToLower()
    Set-Content -Path "$setup.sha256" -Value "$hash  openFTTH-Setup-$version.exe" -Encoding ascii
    Step "$setup ($hash)"
}

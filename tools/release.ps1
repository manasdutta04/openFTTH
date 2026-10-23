<#
.SYNOPSIS
  Builds the Windows release on this PC and publishes it as a GitHub Release, with no GitHub
  Actions minutes: stages the app (tools\package-windows.ps1), builds the installer when Inno
  Setup 6 is installed, always adds a portable zip, writes SHA-256 files, and uploads them with
  docs\releases\v<version>.md as the notes. Versions below 1.0 are published as pre-releases.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\release.ps1            # build and publish
  powershell -File tools\release.ps1 -NoPublish                         # build only
#>
param(
    [switch]$NoPublish,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $repo "dist"
$version = (Select-String -Path (Join-Path $repo "desktop\Cargo.toml") -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$tag = "v$version"
$notes = Join-Path $repo "docs\releases\$tag.md"
if (-not (Test-Path $notes)) { throw "write $notes before releasing" }

$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

$pack = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "package-windows.ps1"))
if ($iscc) { $pack += "-Installer" }
if ($SkipBuild) { $pack += "-SkipBuild" }
& powershell @pack
if ($LASTEXITCODE -ne 0) { throw "packaging failed" }

function Hash($file) {
    $h = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLower()
    Set-Content -Path "$file.sha256" -Value "$h  $(Split-Path -Leaf $file)" -Encoding ascii
}

# The portable zip: unzip anywhere and run openFTTH\openftth.exe.
$zip = Join-Path $dist "openFTTH-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $dist "openFTTH") -DestinationPath $zip -CompressionLevel Optimal
Hash $zip
$assets = @($zip, "$zip.sha256")
$setup = Join-Path $dist "openFTTH-Setup-$version.exe"
if ($iscc -and (Test-Path $setup)) { $assets += @($setup, "$setup.sha256") }
foreach ($a in $assets) { "{0,8:N1} MB  {1}" -f ((Get-Item $a).Length / 1MB), (Split-Path -Leaf $a) }

if ($NoPublish) { return }
$title = "openFTTH $tag" + $(if ($version.StartsWith("0.")) { " (beta)" } else { "" })
# Betas are full releases with "(beta)" in the title: the app's update check reads
# releases/latest, which skips pre-releases.
$flags = @("--title", $title, "--notes-file", $notes, "--latest")
gh release view $tag *> $null
if ($LASTEXITCODE -eq 0) {
    gh release upload $tag @assets --clobber
} else {
    gh release create $tag @assets @flags --verify-tag
}
if ($LASTEXITCODE -ne 0) { throw "publishing $tag failed" }
gh release view $tag --json url --jq .url

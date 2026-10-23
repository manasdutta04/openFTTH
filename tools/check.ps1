<#
.SYNOPSIS
  Every check CI would run, on this PC: engine lint and tests, harness tests, desktop clippy and
  tests, and with -Audit the dependency audits. Costs no GitHub Actions minutes.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\check.ps1
  powershell -File tools\check.ps1 -Audit
  powershell -File tools\check.ps1 -Quick     # skips the slow Gent pipeline tests
#>
param(
    [switch]$Audit,
    [switch]$Quick
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$failed = @()

function Check($name, $dir, [scriptblock]$body) {
    Write-Host "== $name"
    Push-Location (Join-Path $repo $dir)
    try {
        & $body
        if ($LASTEXITCODE -ne 0) { throw "exit $LASTEXITCODE" }
        Write-Host "ok  $name"
    } catch {
        Write-Host "FAIL $name ($_)"
        $script:failed += $name
    } finally {
        Pop-Location
    }
}

Check "engine lint" "engine" { uv run ruff check . }
Check "engine tests" "engine" {
    if ($Quick) { uv run pytest -q -p no:warnings --deselect tests/test_pipeline_gent.py }
    else { uv run pytest -q -p no:warnings }
}
Check "harness tests" "harness" { pnpm test }
Check "desktop clippy" "desktop" { cargo clippy -q --workspace -- -D warnings }
Check "desktop tests" "desktop" { cargo test -q --workspace }

if ($Audit) {
    Check "audit python" "engine" {
        uv export --frozen --no-dev --no-hashes --no-emit-project -o "$env:TEMP\openftth-req.txt"
        uvx pip-audit --strict -r "$env:TEMP\openftth-req.txt"
    }
    Check "audit node" "harness" { pnpm audit --prod --audit-level high }
    Check "audit rust" "desktop" {
        if (-not (Get-Command cargo-audit -ErrorAction SilentlyContinue)) {
            Write-Host "cargo-audit is not installed: cargo install cargo-audit --locked"
            $global:LASTEXITCODE = 1
            return
        }
        cargo audit
    }
}

if ($failed.Count) {
    Write-Host "`nFAILED: $($failed -join ', ')"
    exit 1
}
Write-Host "`nAll checks passed."

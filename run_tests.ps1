# Nova offline test runner (Phase 0.2 of docs/ROADMAP.md)
# Runs every suite in tests/ with the project venv and reports a summary.
# Usage:  .\run_tests.ps1            (all suites)
#         .\run_tests.ps1 memory     (only suites whose name matches "memory")
param([string]$Filter = "")

$repo = $PSScriptRoot
$python = Join-Path $repo "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { Write-Host "venv python not found: $python" -ForegroundColor Red; exit 1 }

# Child stdout is decoded with the console code page, which on this machine is
# cp1252. A suite that prints an em dash or a curly quote then dies with
# UnicodeEncodeError inside its own print() and is reported as a FAILURE, with
# no failing assertion anywhere - test_artifacts_recall_jv2.py has been failing
# the gate this way for reasons that have nothing to do with the code under
# test. UTF-8 on both sides of the pipe.
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONPATH = $repo
$suites = Get-ChildItem (Join-Path $repo "tests") -Filter "test_*.py" | Sort-Object Name
if ($Filter) { $suites = $suites | Where-Object { $_.Name -match $Filter } }

$failed = @()
$passed = 0
# A passing suite gets its usual three lines. A FAILING one gets enough to
# name the assertion that failed: keeping only the tail made every flake
# un-diagnosable, and "it passed when I ran it again" is not a diagnosis.
foreach ($suite in $suites) {
    Write-Host ("-- " + $suite.Name) -ForegroundColor Cyan
    $out = & $python $suite.FullName 2>&1
    if ($LASTEXITCODE -eq 0) {
        $passed++
        $out | Select-Object -Last 3 | ForEach-Object { Write-Host ("   " + $_) }
    } else {
        $failed += $suite.Name
        $hits = $out | Select-String -SimpleMatch "FAIL", "Error", "Traceback"
        if ($hits) { $hits | ForEach-Object { Write-Host ("   " + $_.Line) -ForegroundColor Red } }
        $out | Select-Object -Last 25 | ForEach-Object { Write-Host ("   " + $_) }
    }
}

Write-Host ""
Write-Host ("PASSED: {0}/{1}" -f $passed, $suites.Count) -ForegroundColor Green
if ($failed.Count -gt 0) {
    Write-Host ("FAILED: " + ($failed -join ", ")) -ForegroundColor Red
    exit 1
}
exit 0

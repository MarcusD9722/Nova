# Nova offline test runner (Phase 0.2 of docs/ROADMAP.md)
# Runs every suite in tests/ with the project venv and reports a summary.
# Usage:  .\run_tests.ps1            (all suites)
#         .\run_tests.ps1 memory     (only suites whose name matches "memory")
param([string]$Filter = "")

$repo = $PSScriptRoot
$python = Join-Path $repo "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { Write-Host "venv python not found: $python" -ForegroundColor Red; exit 1 }

$env:PYTHONPATH = $repo
$suites = Get-ChildItem (Join-Path $repo "tests") -Filter "test_*.py" | Sort-Object Name
if ($Filter) { $suites = $suites | Where-Object { $_.Name -match $Filter } }

$failed = @()
$passed = 0
foreach ($suite in $suites) {
    Write-Host ("-- " + $suite.Name) -ForegroundColor Cyan
    & $python $suite.FullName 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host ("   " + $_) }
    if ($LASTEXITCODE -eq 0) { $passed++ } else { $failed += $suite.Name }
}

Write-Host ""
Write-Host ("PASSED: {0}/{1}" -f $passed, $suites.Count) -ForegroundColor Green
if ($failed.Count -gt 0) {
    Write-Host ("FAILED: " + ($failed -join ", ")) -ForegroundColor Red
    exit 1
}
exit 0

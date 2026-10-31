$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Missing .venv. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
Push-Location $root
try {
    & $python qa_haste_buff.py
    if ($LASTEXITCODE -ne 0) { throw 'Haste regression tests failed' }
    & $python overlay_app.py --self-test
    if ($LASTEXITCODE -ne 0) { throw 'Overlay self-test failed' }
    & $python overlay_app.py --ui-test
    if ($LASTEXITCODE -ne 0) { throw 'Overlay UI test failed' }
    & $python qa_overlay_periods.py
    if ($LASTEXITCODE -ne 0) { throw 'Timed EXP QA failed' }

    # PyInstaller logs progress to stderr, which PowerShell 5 can wrap as
    # a NativeCommandError. Trust the native exit code instead.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python -m PyInstaller --noconfirm --clean MapleHuntTracker_v1_9_4.spec
        $result = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($result -ne 0) { throw "EXE build failed: $result" }
    Write-Output ('BUILT_EXE=' + (Join-Path $root 'dist\MapleHuntTracker_v1_9_4.exe'))
} finally {
    Pop-Location
}

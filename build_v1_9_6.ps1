$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Missing .venv. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
Push-Location $root
try {
    & $python qa_exp_integrity.py
    if ($LASTEXITCODE -ne 0) { throw 'EXP integrity tests failed' }
    & $python qa_exp_picker.py
    if ($LASTEXITCODE -ne 0) { throw 'EXP zoom picker tests failed' }
    & $python qa_haste_buff.py
    if ($LASTEXITCODE -ne 0) { throw 'Haste tests failed' }
    & $python overlay_app.py --self-test
    if ($LASTEXITCODE -ne 0) { throw 'Overlay self-test failed' }
    & $python overlay_app.py --ui-test
    if ($LASTEXITCODE -ne 0) { throw 'Overlay UI test failed' }

    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python -m PyInstaller --noconfirm --clean MapleHuntTracker_v1_9_6.spec
        $result = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($result -ne 0) { throw "EXE build failed: $result" }
    Write-Output ('BUILT_EXE=' + (Join-Path $root 'dist\MapleHuntTracker_v1_9_6.exe'))
} finally {
    Pop-Location
}

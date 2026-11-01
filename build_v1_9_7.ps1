$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Missing .venv. Run: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
Push-Location $root
try {
    foreach ($test in @('qa_exp_integrity.py', 'qa_ocr_stability.py',
                        'qa_exp_picker.py', 'qa_haste_buff.py')) {
        & $python $test
        if ($LASTEXITCODE -ne 0) { throw "$test failed" }
    }
    & $python overlay_app.py --self-test
    if ($LASTEXITCODE -ne 0) { throw 'Overlay self-test failed' }
    & $python overlay_app.py --ui-test
    if ($LASTEXITCODE -ne 0) { throw 'Overlay UI test failed' }

    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python -m PyInstaller --noconfirm --clean MapleHuntTracker_v1_9_7.spec
        $result = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($result -ne 0) { throw "EXE build failed: $result" }
    Write-Output ('BUILT_EXE=' + (Join-Path $root 'dist\MapleHuntTracker_v1_9_7.exe'))
} finally {
    Pop-Location
}

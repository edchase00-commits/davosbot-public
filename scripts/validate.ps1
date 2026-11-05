$ErrorActionPreference = "Stop"

$env:DAVOSBOT_SUPPRESS_CONFIG_WARNINGS = "1"

$pythonBin = $env:PYTHON
if (-not $pythonBin) {
    $venvPython = Join-Path (Get-Location) "venv\Scripts\python.exe"
    $dotVenvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    $prodDir = $env:DAVOSBOT_PROD_DIR
    if (-not $prodDir -and $env:HOME) {
        $defaultProdDir = Join-Path $env:HOME "projects/davosbot"
        if (Test-Path $defaultProdDir) {
            $prodDir = $defaultProdDir
        }
    }
    $prodVenvWindows = if ($prodDir) { Join-Path $prodDir "venv\Scripts\python.exe" } else { $null }
    $prodDotVenvWindows = if ($prodDir) { Join-Path $prodDir ".venv\Scripts\python.exe" } else { $null }
    $prodVenvUnix = if ($prodDir) { Join-Path $prodDir "venv/bin/python" } else { $null }
    $prodDotVenvUnix = if ($prodDir) { Join-Path $prodDir ".venv/bin/python" } else { $null }
    if (Test-Path $venvPython) {
        $pythonBin = $venvPython
    } elseif (Test-Path $dotVenvPython) {
        $pythonBin = $dotVenvPython
    } elseif ($prodVenvWindows -and (Test-Path $prodVenvWindows)) {
        $pythonBin = $prodVenvWindows
    } elseif ($prodDotVenvWindows -and (Test-Path $prodDotVenvWindows)) {
        $pythonBin = $prodDotVenvWindows
    } elseif ($prodVenvUnix -and (Test-Path $prodVenvUnix)) {
        $pythonBin = $prodVenvUnix
    } elseif ($prodDotVenvUnix -and (Test-Path $prodDotVenvUnix)) {
        $pythonBin = $prodDotVenvUnix
    } else {
        $pythonBin = "python"
    }
}

& $pythonBin -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& $pythonBin -m compileall -q main.py davosbot scripts tests
exit $LASTEXITCODE

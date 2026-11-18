# Run the isolated, official ARTIQ environment without changing global PATH.
# Example: .\scripts\artiq-python.ps1 -m artiq.frontend.artiq_compile --version
$ErrorActionPreference = 'Stop'
$toolchainRoot = Join-Path (Split-Path $PSScriptRoot -Parent) '.toolchains/artiq9'
$toolchainPython = Join-Path $toolchainRoot 'clang64/bin/python.exe'
if (-not (Test-Path -LiteralPath $toolchainPython)) {
    throw "ARTIQ toolchain not found at $toolchainRoot. See docs/artiq_toolchain.md."
}
$previousPath = $env:PATH
$previousEncoding = $env:PYTHONIOENCODING
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
    $env:PATH = "$(Join-Path $toolchainRoot 'clang64/bin');$(Join-Path $toolchainRoot 'usr/bin');$previousPath"
    $env:PYTHONIOENCODING = 'utf-8'
    $toolchainTemp = Join-Path $toolchainRoot 'tmp/taiyidsl'
    New-Item -ItemType Directory -Force -Path $toolchainTemp | Out-Null
    $env:TEMP = $toolchainTemp
    $env:TMP = $toolchainTemp
    & $toolchainPython @args
    $toolchainExitCode = $LASTEXITCODE
} finally {
    $env:PATH = $previousPath
    $env:PYTHONIOENCODING = $previousEncoding
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
}
exit $toolchainExitCode

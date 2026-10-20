[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ToolArgs)
$ErrorActionPreference = 'Stop'
$recordPath = Join-Path $env:LOCALAPPDATA 'AutoVideoEdit\PremiereReview\current_install.json'
if (-not (Test-Path -LiteralPath $recordPath)) { throw 'Run install.ps1 -Apply first.' }
$record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $record.python) -or -not (Test-Path -LiteralPath $record.cli)) { throw 'Installed runtime is missing; run installation again.' }
$previousBridgeDirectory = $env:AVE_BRIDGE_DIR
try {
    if ($record.bridge_dir) { $env:AVE_BRIDGE_DIR = $record.bridge_dir }
    & $record.python $record.cli @ToolArgs
    $toolExitCode = $LASTEXITCODE
} finally {
    $env:AVE_BRIDGE_DIR = $previousBridgeDirectory
}
exit $toolExitCode

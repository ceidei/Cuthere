[CmdletBinding()]
param([switch]$Apply, [switch]$RestorePrevious)
$ErrorActionPreference = 'Stop'
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'AutoVideoEdit\PremiereReview'))
$extensionRoot = [IO.Path]::GetFullPath((Join-Path $env:APPDATA 'Adobe\CEP\extensions'))
$recordPath = Join-Path $runtimeRoot 'current_install.json'
if (-not (Test-Path -LiteralPath $recordPath)) { Write-Host 'No installation record.'; exit 0 }
$record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($panel in $record.panels) {
    $dest = [IO.Path]::GetFullPath($panel.destination)
    $backup = [IO.Path]::GetFullPath($panel.backup)
    if (-not $dest.StartsWith($extensionRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or -not $backup.StartsWith($runtimeRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Installation record path is outside expected directories.' }
}
foreach ($panel in @($record.retired_panels)) {
    if ($null -eq $panel) { continue }
    $dest = [IO.Path]::GetFullPath($panel.destination)
    $backup = [IO.Path]::GetFullPath($panel.backup)
    if (-not $dest.StartsWith($extensionRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or -not $backup.StartsWith($runtimeRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Legacy panel record path is outside expected directories.' }
    if ($RestorePrevious -and (Test-Path -LiteralPath $dest)) { throw "Restore destination already exists: $dest" }
}
$previousRecordPath = $null
if ($RestorePrevious -and $record.previous_install_record) {
    $previousRecordPath = [IO.Path]::GetFullPath($record.previous_install_record)
    if (-not $previousRecordPath.StartsWith($runtimeRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $previousRecordPath)) { throw 'Previous installation record is missing or outside runtime directory.' }
}
if (-not $Apply) { $record | ConvertTo-Json -Depth 6; Write-Host 'DRY_RUN: panel will be moved to backup; -RestorePrevious also restores the previous capture, bridge and runtime record.'; exit 0 }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
foreach ($panel in $record.panels) {
    $retired = Join-Path $runtimeRoot ('backups\removed_' + (Split-Path -Leaf $panel.destination) + '_' + $stamp)
    New-Item -ItemType Directory -Path (Split-Path -Parent $retired) -Force | Out-Null
    if (Test-Path -LiteralPath $panel.destination) { Move-Item -LiteralPath $panel.destination -Destination $retired }
    if ($RestorePrevious -and (Test-Path -LiteralPath $panel.backup)) { Move-Item -LiteralPath $panel.backup -Destination $panel.destination }
}
if ($RestorePrevious) {
    foreach ($panel in @($record.retired_panels)) {
        if ($null -eq $panel) { continue }
        if (Test-Path -LiteralPath $panel.backup) { Move-Item -LiteralPath $panel.backup -Destination $panel.destination }
    }
}
Move-Item -LiteralPath $recordPath -Destination (Join-Path $runtimeRoot ('uninstalled_' + $stamp + '.json'))
if ($previousRecordPath) { Copy-Item -LiteralPath $previousRecordPath -Destination $recordPath }
Write-Host 'Panels disabled and preserved. Close/reopen Premiere to reload extensions.'

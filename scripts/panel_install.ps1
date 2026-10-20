# Filesystem-only CEP deployment. Network/runtime setup stays in install.ps1.
function Assert-F8ChildPath([string]$Path, [string]$Root) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Path leaves the expected directory.' }
    return $resolved
}

function Assert-F8Bundle([string]$Path, [string]$BundleId) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $manifestPath = Join-Path $Path 'CSXS\manifest.xml'
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Existing extension has no manifest: $Path" }
    [xml]$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
    if ($manifest.ExtensionManifest.ExtensionBundleId -ne $BundleId) { throw "Existing extension belongs to another bundle: $Path" }
}

function Restore-F8PanelSet($Deployment, [string]$RuntimeRoot, [string]$ExtensionRoot, [string]$Stamp) {
    foreach ($entry in @($Deployment.panels)) {
        $dest = Assert-F8ChildPath $entry.destination $ExtensionRoot
        $backup = Assert-F8ChildPath $entry.backup $RuntimeRoot
        if (Test-Path -LiteralPath $dest) {
            $failed = Assert-F8ChildPath (Join-Path $RuntimeRoot ('backups\failed_' + (Split-Path -Leaf $dest) + '_' + $Stamp)) $RuntimeRoot
            Move-Item -LiteralPath $dest -Destination $failed
        }
        if (Test-Path -LiteralPath $backup) { Move-Item -LiteralPath $backup -Destination $dest }
    }
    foreach ($entry in @($Deployment.retired_panels)) {
        $dest = Assert-F8ChildPath $entry.destination $ExtensionRoot
        $backup = Assert-F8ChildPath $entry.backup $RuntimeRoot
        if (Test-Path -LiteralPath $backup) { Move-Item -LiteralPath $backup -Destination $dest }
    }
}

function Install-F8PanelSet([string]$ReleaseRoot, [string]$ExtensionRoot,
                           [string]$RuntimeRoot, [string]$VersionRoot,
                           $Config, [string]$Stamp) {
    $dest = Assert-F8ChildPath (Join-Path $ExtensionRoot 'AutoVideoEditReviewCapture') $ExtensionRoot
    $legacy = Assert-F8ChildPath (Join-Path $ExtensionRoot 'autovideoedit') $ExtensionRoot
    Assert-F8Bundle $dest 'com.autovideoedit.reviewcapture'
    Assert-F8Bundle $legacy 'com.autovideoedit.hermes'
    $stage = Assert-F8ChildPath (Join-Path $VersionRoot 'panel_stage') $RuntimeRoot
    if (Test-Path -LiteralPath $stage) { throw 'Installation stage already exists.' }
    Copy-Item -LiteralPath (Join-Path $ReleaseRoot 'premiere_cep\review_capture_panel') -Destination $stage -Recurse
    [IO.File]::WriteAllText((Join-Path $stage 'runtime.json'), ($Config | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    $deployment = @{panels=@();retired_panels=@()}
    New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot 'backups') -Force | Out-Null
    New-Item -ItemType Directory -Path $ExtensionRoot -Force | Out-Null
    try {
        if (Test-Path -LiteralPath $legacy) {
            $backup = Assert-F8ChildPath (Join-Path $RuntimeRoot ('backups\autovideoedit_' + $Stamp)) $RuntimeRoot
            if (Test-Path -LiteralPath $backup) { throw 'Legacy backup path already exists.' }
            Move-Item -LiteralPath $legacy -Destination $backup
            $deployment.retired_panels += @{destination=$legacy;backup=$backup}
        }
        $backup = Assert-F8ChildPath (Join-Path $RuntimeRoot ('backups\AutoVideoEditReviewCapture_' + $Stamp)) $RuntimeRoot
        if (Test-Path -LiteralPath $backup) { throw 'Panel backup path already exists.' }
        if (Test-Path -LiteralPath $dest) { Move-Item -LiteralPath $dest -Destination $backup }
        $deployment.panels += @{destination=$dest;backup=$backup}
        Move-Item -LiteralPath $stage -Destination $dest
    } catch {
        Restore-F8PanelSet $deployment $RuntimeRoot $ExtensionRoot $Stamp
        throw
    }
    return $deployment
}

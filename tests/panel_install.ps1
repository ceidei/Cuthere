param([Parameter(Mandatory=$true)][string]$TestRoot)
$ErrorActionPreference = 'Stop'
$releaseRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $releaseRoot 'scripts\panel_install.ps1')
if (Test-Path -LiteralPath $TestRoot) { throw 'Use a fresh test directory.' }
New-Item -ItemType Directory -Path $TestRoot | Out-Null
function Assert-Test($Value, [string]$Message) {if (-not $Value) {throw $Message}}
function New-Fixture([string]$Name) {
    $base = Join-Path $TestRoot $Name
    $ext = Join-Path $base 'roaming\Adobe\CEP\extensions'
    $runtime = Join-Path $base 'local\AutoVideoEdit\PremiereReview'
    $version = Join-Path $runtime 'versions\rc2'
    New-Item -ItemType Directory -Path $version -Force | Out-Null
    foreach ($pair in @(@('AutoVideoEditReviewCapture','com.autovideoedit.reviewcapture'), @('autovideoedit','com.autovideoedit.hermes'))) {
        $folder = Join-Path $ext $pair[0]
        New-Item -ItemType Directory -Path (Join-Path $folder 'CSXS') -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $folder 'CSXS\manifest.xml'), ('<ExtensionManifest ExtensionBundleId="' + $pair[1] + '"/>'))
        [IO.File]::WriteAllText((Join-Path $folder 'original.txt'), $pair[0])
    }
    return @{ext=$ext;runtime=$runtime;version=$version}
}
$f = New-Fixture 'migration'
$deployment = Install-F8PanelSet $releaseRoot $f.ext $f.runtime $f.version @{bridge_dir='C:\Test Bridge'} 'test'
Assert-Test ($deployment.panels.Count -eq 1) 'Expected one installed panel.'
Assert-Test ($deployment.retired_panels.Count -eq 1) 'Expected legacy bridge backup.'
Assert-Test (-not (Test-Path -LiteralPath (Join-Path $f.ext 'autovideoedit'))) 'Legacy bridge still registered.'
Assert-Test ((Get-ChildItem -LiteralPath $f.ext -Directory).Count -eq 1) 'Multiple installed extension folders.'
$config = Get-Content -LiteralPath (Join-Path $f.ext 'AutoVideoEditReviewCapture\runtime.json') -Raw | ConvertFrom-Json
Assert-Test ($config.bridge_dir -eq 'C:\Test Bridge') 'Runtime config missing.'
Restore-F8PanelSet $deployment $f.runtime $f.ext 'rollback'
foreach ($name in @('AutoVideoEditReviewCapture','autovideoedit')) {
    Assert-Test (Test-Path -LiteralPath (Join-Path $f.ext ($name + '\original.txt'))) 'Previous panel was not restored.'
}
$f = New-Fixture 'failure'
$script:failOnce = $true
function Move-Item {
    param([string]$LiteralPath, [string]$Destination)
    if ($script:failOnce -and (Split-Path -Leaf $LiteralPath) -eq 'panel_stage') {
        $script:failOnce = $false
        throw 'Simulated deployment failure'
    }
    Microsoft.PowerShell.Management\Move-Item -LiteralPath $LiteralPath -Destination $Destination
}
$rejected = $false
try {Install-F8PanelSet $releaseRoot $f.ext $f.runtime $f.version @{} 'failure' | Out-Null} catch {$rejected = $_.Exception.Message -match 'Simulated'}
Assert-Test $rejected 'Failure was not propagated.'
foreach ($name in @('AutoVideoEditReviewCapture','autovideoedit')) {
    Assert-Test (Test-Path -LiteralPath (Join-Path $f.ext ($name + '\original.txt'))) 'Failed install did not restore previous panels.'
}
$f = New-Fixture 'unrelated'
[IO.File]::WriteAllText((Join-Path $f.ext 'autovideoedit\CSXS\manifest.xml'), '<ExtensionManifest ExtensionBundleId="another.vendor"/>')
$rejected = $false
try {Install-F8PanelSet $releaseRoot $f.ext $f.runtime $f.version @{} 'unrelated' | Out-Null} catch {$rejected = $_.Exception.Message -match 'another bundle'}
Assert-Test $rejected 'Unrelated extension was not protected.'
Assert-Test (Test-Path -LiteralPath (Join-Path $f.ext 'AutoVideoEditReviewCapture\original.txt')) 'Preflight changed an existing panel.'
$f = New-Fixture 'uninstall'
$deployment = Install-F8PanelSet $releaseRoot $f.ext $f.runtime $f.version @{} 'uninstall'
$previousRecordPath = Join-Path $f.version 'previous_install.json'
[IO.File]::WriteAllText($previousRecordPath, '{"version":"previous","panels":[],"retired_panels":[]}')
$record = @{panels=@($deployment.panels);retired_panels=@($deployment.retired_panels);previous_install_record=$previousRecordPath}
[IO.File]::WriteAllText((Join-Path $f.runtime 'current_install.json'), ($record | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
$previousAppData = $env:APPDATA
$previousLocalAppData = $env:LOCALAPPDATA
try {
    $env:APPDATA = Join-Path $TestRoot 'uninstall\roaming'
    $env:LOCALAPPDATA = Join-Path $TestRoot 'uninstall\local'
    & (Get-Process -Id $PID).Path -NoProfile -ExecutionPolicy Bypass -File (Join-Path $releaseRoot 'uninstall.ps1') -Apply -RestorePrevious
    Assert-Test ($LASTEXITCODE -eq 0) 'Uninstall failed.'
} finally {
    $env:APPDATA = $previousAppData
    $env:LOCALAPPDATA = $previousLocalAppData
}
foreach ($name in @('AutoVideoEditReviewCapture','autovideoedit')) {
    Assert-Test (Test-Path -LiteralPath (Join-Path $f.ext ($name + '\original.txt'))) 'Uninstall did not restore previous panels.'
}
$restoredRecord = Get-Content -LiteralPath (Join-Path $f.runtime 'current_install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Assert-Test ($restoredRecord.version -eq 'previous') 'Uninstall did not restore the previous runtime record.'
$base = Join-Path $TestRoot 'clean'
$ext = Join-Path $base 'extensions'
$runtime = Join-Path $base 'runtime'
$version = Join-Path $runtime 'versions\rc2'
New-Item -ItemType Directory -Path $version -Force | Out-Null
$deployment = Install-F8PanelSet $releaseRoot $ext $runtime $version @{} 'clean'
Assert-Test ($deployment.panels.Count -eq 1 -and $deployment.retired_panels.Count -eq 0) 'Clean deployment failed.'
Restore-F8PanelSet $deployment $runtime $ext 'clean-restore'
Assert-Test (-not (Test-Path -LiteralPath (Join-Path $ext 'AutoVideoEditReviewCapture'))) 'Clean rollback did not disable panel.'
Write-Output 'PASS: one-panel migration, restore, failure rollback, unrelated bundle protection, uninstall restore, clean deployment.'

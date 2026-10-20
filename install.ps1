[CmdletBinding()]
param([switch]$Apply, [switch]$DownloadModel,
      [ValidateSet('tiny','base','small','medium')][string]$Model = 'medium',
      [ValidatePattern('^[0-9]+$')][string]$CepVersion = '12')
$ErrorActionPreference = 'Stop'
$releaseRoot = $PSScriptRoot
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'AutoVideoEdit\PremiereReview'
$extensionRoot = [IO.Path]::GetFullPath((Join-Path $env:APPDATA 'Adobe\CEP\extensions'))
$version = (Get-Content -LiteralPath (Join-Path $releaseRoot 'VERSION') -Raw).Trim()
if (-not $Apply) {
    [ordered]@{mode='DRY_RUN';version=$version;runtime=$runtimeRoot;extensions=$extensionRoot;
        panels=@(@{source='review_capture_panel';target='AutoVideoEditReviewCapture'});
        retire_legacy_bridge='autovideoedit (only matching com.autovideoedit.hermes bundle)';
        python='3.11';network='pip installs requirements.txt; model downloads only with -DownloadModel';
        model=$Model;registry="HKCU:\Software\Adobe\CSXS.$CepVersion";
        preserve='Existing capture and legacy bridge move to versioned backups'} | ConvertTo-Json -Depth 5
    exit 0
}
. (Join-Path $releaseRoot 'scripts\panel_install.ps1')
$launcher = Get-Command py.exe -ErrorAction Stop
& $launcher.Source -3.11 -c 'import sys; assert sys.version_info[:2] == (3,11)'
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 with the Python Launcher first.' }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$versionRoot = Join-Path $runtimeRoot "versions\f8-$version-$stamp"
New-Item -ItemType Directory -Path $versionRoot | Out-Null
$logPath = Join-Path $versionRoot 'install.log'
function Log([string]$Message) { Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $Message" -Encoding utf8; Write-Host $Message }
function Write-Json([string]$Path, $Value) { [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false)) }
$codeRoot = Join-Path $versionRoot 'code'
New-Item -ItemType Directory -Path $codeRoot | Out-Null
foreach ($dir in @('engine','services','scripts','docs')) {
    Copy-Item -LiteralPath (Join-Path $releaseRoot $dir) -Destination (Join-Path $codeRoot $dir) -Recurse
}
Copy-Item -LiteralPath (Join-Path $releaseRoot 'VERSION') -Destination (Join-Path $codeRoot 'VERSION')
$venv = Join-Path $versionRoot 'venv'
& $launcher.Source -3.11 -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
$python = Join-Path $venv 'Scripts\python.exe'
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
& $python -m pip install --disable-pip-version-check -r (Join-Path $releaseRoot 'requirements.txt') 2>&1 | Tee-Object -FilePath (Join-Path $versionRoot 'pip.log')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; panels have not been replaced.' }
if ($DownloadModel) {
    & $python -c 'import sys; from huggingface_hub import snapshot_download; snapshot_download(repo_id="Systran/faster-whisper-"+sys.argv[1])' $Model
    if ($LASTEXITCODE -ne 0) { throw 'Model download failed; panels have not been replaced.' }
}
& $python -c 'import sys; sys.path.insert(0,sys.argv[1]); from scripts.transcribe_review_batch import find_cached_model; print(find_cached_model(sys.argv[2]))' $codeRoot $Model
if ($LASTEXITCODE -ne 0) { throw 'Transcription model not cached. Re-run with -DownloadModel. Panels have not been replaced.' }
$ipc = Join-Path $runtimeRoot "ipc\f8-$version"
New-Item -ItemType Directory -Path $ipc -Force | Out-Null
$bridge = if ($env:AVE_BRIDGE_DIR) {[IO.Path]::GetFullPath($env:AVE_BRIDGE_DIR)} else {'C:\Users\Public\Documents\ave-bridge'}
foreach ($name in @('commands','responses','locks')) { New-Item -ItemType Directory -Path (Join-Path $bridge $name) -Force | Out-Null }
$registry = "HKCU:\Software\Adobe\CSXS.$CepVersion"
New-Item -Path $registry -Force | Out-Null
New-ItemProperty -Path $registry -Name PlayerDebugMode -Value '1' -PropertyType String -Force | Out-Null
$config = [ordered]@{schema_version='1.0';pythonw=$pythonw;
    worker_entry=(Join-Path $codeRoot 'services\review_capture\worker_entry.py');
    visibility_entry=(Join-Path $codeRoot 'services\review_capture\show_panel.py');ipc_root=$ipc;
    transcription_python=$pythonw;transcription_entry=(Join-Path $codeRoot 'scripts\transcribe_review_batch.py');
    note_transcription_entry=(Join-Path $codeRoot 'scripts\transcribe_review_note.py');
    transcription_model=$Model;bridge_dir=$bridge;build="f8-$version";installed_at=(Get-Date).ToString('o')}
$deployment = Install-F8PanelSet $releaseRoot $extensionRoot $runtimeRoot $versionRoot $config $stamp
$recordPath = Join-Path $runtimeRoot 'current_install.json'
$previousRecord = if (Test-Path -LiteralPath $recordPath) {[IO.File]::ReadAllText($recordPath)} else {$null}
try {
    $previousRecordPath = $null
    if ($null -ne $previousRecord) {
        $previousRecordPath = Join-Path $versionRoot 'previous_install.json'
        [IO.File]::WriteAllText($previousRecordPath, $previousRecord, [Text.UTF8Encoding]::new($false))
    }
    $record = [ordered]@{version=$version;python=$python;cli=(Join-Path $codeRoot 'scripts\cli.py');runtime=$versionRoot;
        panels=@($deployment.panels);retired_panels=@($deployment.retired_panels);bridge_dir=$bridge;model=$Model;
        previous_install_record=$previousRecordPath}
    Write-Json (Join-Path $versionRoot 'install_receipt.json') $record
    Write-Json $recordPath $record
} catch {
    Restore-F8PanelSet $deployment $runtimeRoot $extensionRoot $stamp
    if ($null -ne $previousRecord) {[IO.File]::WriteAllText($recordPath, $previousRecord, [Text.UTF8Encoding]::new($false))}
    elseif (Test-Path -LiteralPath $recordPath) {Move-Item -LiteralPath $recordPath -Destination (Join-Path $versionRoot 'failed_install_record.json')}
    throw
}
Log 'Installed one Cuthere panel. Restart Premiere and open Cuthere. The edit bridge starts automatically.'

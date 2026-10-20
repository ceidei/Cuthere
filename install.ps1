[CmdletBinding()]
param([switch]$Apply, [switch]$DownloadModel,
      [ValidateSet('tiny','base','small','medium')][string]$Model = 'medium',
      [ValidatePattern('^[0-9]+$')][string]$CepVersion = '12',
      # Transcription language recorded for the panel; 'auto' detects it from the audio.
      [string]$Language = 'auto')
$ErrorActionPreference = 'Stop'
$releaseRoot = $PSScriptRoot
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'AutoVideoEdit\PremiereReview'
$extensionRoot = [IO.Path]::GetFullPath((Join-Path $env:APPDATA 'Adobe\CEP\extensions'))
$version = (Get-Content -LiteralPath (Join-Path $releaseRoot 'VERSION') -Raw).Trim()
# The plan reports the language that an apply would actually record: an
# explicit -Language, an earlier installation's saved value, or auto.
$planLanguage = $Language
$planLanguageSource = if ($PSBoundParameters.ContainsKey('Language')) {'explicit'} else {'default-auto'}
if (-not $PSBoundParameters.ContainsKey('Language')) {
    $planConfigPath = Join-Path $extensionRoot 'AutoVideoEditReviewCapture\runtime.json'
    if (Test-Path -LiteralPath $planConfigPath) {
        try {
            $planConfig = Get-Content -LiteralPath $planConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $planRecorded = ''
            if ($planConfig.transcription_language) { $planRecorded = [string]$planConfig.transcription_language }
            elseif ($planConfig.language) { $planRecorded = [string]$planConfig.language }
            if ($planRecorded -and $planRecorded -match '^(auto|[a-z]{2,3}([-_][A-Za-z0-9]{2,8})*)$') {
                $planLanguage = $planRecorded
                $planLanguageSource = 'previous-install'
            }
        } catch { }
    }
}
if (-not $Apply) {
    [ordered]@{mode='DRY_RUN';version=$version;runtime=$runtimeRoot;extensions=$extensionRoot;
        panels=@(@{source='review_capture_panel';target='AutoVideoEditReviewCapture'});
        retire_legacy_bridge='autovideoedit (only matching com.autovideoedit.hermes bundle)';
        python='3.11';network='pip installs requirements.txt; model downloads only with -DownloadModel';
        model=$Model;language=$planLanguage;language_source=$planLanguageSource;
        registry="HKCU:\Software\Adobe\CSXS.$CepVersion";
        preserve='Existing capture and legacy bridge move to versioned backups'} | ConvertTo-Json -Depth 5
    exit 0
}
. (Join-Path $releaseRoot 'scripts\panel_install.ps1')
$launcher = Get-Command py.exe -ErrorAction Stop
& $launcher.Source -3.11 -c 'import sys; assert sys.version_info[:2] == (3,11)'
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 with the Python Launcher first.' }
if ($Language -notmatch '^(auto|[a-z]{2,3}([-_][A-Za-z0-9]{2,8})*)$') { throw "Use -Language auto or an ISO code such as zh/en (got '$Language')." }
# Where the recorded language came from: an explicit option, an earlier
# installation, or the fresh-install default. Nothing is migrated silently.
$languageSource = if ($PSBoundParameters.ContainsKey('Language')) {'explicit'} else {'default-auto'}
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
$config = [ordered]@{schema_version='1.0';pythonw=$pythonw;launcher=$pythonw;platform='win32';
    worker_entry=(Join-Path $codeRoot 'services\review_capture\worker_entry.py');
    visibility_entry=(Join-Path $codeRoot 'services\review_capture\show_panel.py');ipc_root=$ipc;
    transcription_python=$pythonw;transcription_entry=(Join-Path $codeRoot 'scripts\transcribe_review_batch.py');
    note_transcription_entry=(Join-Path $codeRoot 'scripts\transcribe_review_note.py');
    transcription_model=$Model;transcription_language=$Language;language=$Language;
    language_source=$languageSource;bridge_dir=$bridge;
    build="f8-$version";installed_at=(Get-Date).ToString('o')}
# An existing installation keeps its recorded language. 1.0 did not write one,
# so an upgrade from 1.0 uses auto detection (not a silent rewrite of a saved
# preference). The old runtime.json and record are copied for audit before the
# previous panel is moved.
$previousConfigPath = Join-Path $extensionRoot 'AutoVideoEditReviewCapture\runtime.json'
if (Test-Path -LiteralPath $previousConfigPath) {
    Copy-Item -LiteralPath $previousConfigPath -Destination (Join-Path $versionRoot 'previous_runtime.json') -Force
    if (-not $PSBoundParameters.ContainsKey('Language')) {
        try {
            $previousConfig = Get-Content -LiteralPath $previousConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $recordedLanguage = ''
            if ($previousConfig.transcription_language) { $recordedLanguage = [string]$previousConfig.transcription_language }
            elseif ($previousConfig.language) { $recordedLanguage = [string]$previousConfig.language }
            if ($recordedLanguage -and $recordedLanguage -match '^(auto|[a-z]{2,3}([-_][A-Za-z0-9]{2,8})*)$') {
                $Language = $recordedLanguage
                $languageSource = 'previous-install'
                $config.transcription_language = $Language
                $config.language = $Language
                $config.language_source = $languageSource
            }
        } catch { Write-Warning 'Previous runtime.json could not be read; recording language=auto.' }
    }
}
$deployment = Install-F8PanelSet $releaseRoot $extensionRoot $runtimeRoot $versionRoot $config $stamp
$recordPath = Join-Path $runtimeRoot 'current_install.json'
$previousRecord = if (Test-Path -LiteralPath $recordPath) {[IO.File]::ReadAllText($recordPath)} else {$null}
try {
    $previousRecordPath = $null
    if ($null -ne $previousRecord) {
        $previousRecordPath = Join-Path $versionRoot 'previous_install.json'
        [IO.File]::WriteAllText($previousRecordPath, $previousRecord, [Text.UTF8Encoding]::new($false))
    }
    $record = [ordered]@{version=$version;platform='win32';python=$python;launcher=$pythonw;
        cli=(Join-Path $codeRoot 'scripts\cli.py');runtime=$versionRoot;
        panels=@($deployment.panels);retired_panels=@($deployment.retired_panels);bridge_dir=$bridge;model=$Model;
        language=$Language;language_source=$languageSource;previous_install_record=$previousRecordPath}
    Write-Json (Join-Path $versionRoot 'install_receipt.json') $record
    Write-Json $recordPath $record
} catch {
    Restore-F8PanelSet $deployment $runtimeRoot $extensionRoot $stamp
    if ($null -ne $previousRecord) {[IO.File]::WriteAllText($recordPath, $previousRecord, [Text.UTF8Encoding]::new($false))}
    elseif (Test-Path -LiteralPath $recordPath) {Move-Item -LiteralPath $recordPath -Destination (Join-Path $versionRoot 'failed_install_record.json')}
    throw
}
Log 'Installed one Cuthere panel. Restart Premiere and open Cuthere. The edit bridge starts automatically.'

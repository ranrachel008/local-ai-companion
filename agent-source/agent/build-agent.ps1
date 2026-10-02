param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [string]$OutputRoot
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot) -ErrorAction Stop).Path
$packageName = 'Local-AI-Companion-Agent-0.1.17-agent.1'
$specPath = Join-Path $PSScriptRoot ($packageName + '.spec')
$guiInfoPath = Join-Path $project 'BUILD_INFO.json'
$utf8 = [System.Text.UTF8Encoding]::new($false)
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}
if (-not (Test-Path -LiteralPath $specPath -PathType Leaf)) {
    throw "Agent spec not found: $specPath"
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $PSScriptRoot 'out'
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)

function Get-TextSha256([string]$Value) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes($Value)))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-SourceManifest {
    # Inputs are first-party root Python, Agent source/spec/helper/docs/config,
    # original GUI metadata, the three bundled catalogs, and all locale assets.
    # Generated outputs are not source inputs and are never enumerated.
    $files = @()
    $files += Get-ChildItem -LiteralPath $project -Filter '*.py' -File -ErrorAction Stop
    $files += Get-ChildItem -LiteralPath $PSScriptRoot -File -ErrorAction Stop |
        Where-Object { $_.Extension -in @('.py', '.spec', '.ps1', '.md', '.json') -and $_.Name -ne 'AGENT_BUILD_INFO.json' }
    $files += Get-Item -LiteralPath $guiInfoPath -ErrorAction Stop
    foreach ($name in @('radar_sources.json', 'curated_sources.json', 'first_party_youtube_index.json')) {
        $files += Get-Item -LiteralPath (Join-Path $project ('data\' + $name)) -ErrorAction Stop
    }
    $files += Get-ChildItem -LiteralPath (Join-Path $project 'locales') -File -Recurse -ErrorAction Stop
    $paths = [System.Collections.Generic.SortedDictionary[string, string]]::new([System.StringComparer]::Ordinal)
    foreach ($file in $files) {
        $fullPath = [System.IO.Path]::GetFullPath($file.FullName)
        if (-not $fullPath.StartsWith($project + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Source input escaped project directory: $fullPath"
        }
        $relative = $fullPath.Substring($project.Length + 1).Replace('\', '/')
        $paths[$relative] = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    }
    $manifest = [ordered]@{}
    foreach ($entry in $paths.GetEnumerator()) { $manifest[$entry.Key] = $entry.Value }
    return $manifest
}

function Get-ManifestFingerprint($Manifest) {
    $lines = @($Manifest.GetEnumerator() | ForEach-Object { $_.Key + ':' + $_.Value })
    return Get-TextSha256 ($lines -join "`n")
}

function Assert-SourceUnchanged($Expected, [string]$Stage) {
    $observed = Get-SourceManifest
    if ((Get-ManifestFingerprint $observed) -ne (Get-ManifestFingerprint $Expected)) {
        throw "Source inputs changed $Stage; Agent build is not verified"
    }
}

# Inspect the explicitly supplied, existing build runtime; never install it.
$versionProbe = "import json,sys,packaging,PyInstaller; print(json.dumps(dict(python='.'.join(map(str,sys.version_info[:3])),packaging=packaging.__version__,pyinstaller=PyInstaller.__version__)))"
$versionJson = & $Python -c $versionProbe
if ($LASTEXITCODE -ne 0) { throw "Build runtime inspection failed with exit code $LASTEXITCODE" }
$versions = ($versionJson -join "`n") | ConvertFrom-Json -ErrorAction Stop
if ($versions.python -ne '3.10.11' -or $versions.packaging -ne '26.3' -or $versions.pyinstaller -ne '6.22.3') {
    throw ('Unsupported build runtime: ' + ($versions | ConvertTo-Json -Compress))
}

$guiInfo = Get-Content -LiteralPath $guiInfoPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
if ($guiInfo.app_version -ne '0.1.17' -or $guiInfo.build_id -ne '20261001T182710Z-030c49e0') {
    throw ('Unexpected GUI baseline identity: ' + [string]$guiInfo.build_id)
}
foreach ($property in $guiInfo.first_party_python_sha256.PSObject.Properties) {
    $modulePath = Join-Path $project $property.Name
    $observedHash = (Get-FileHash -LiteralPath $modulePath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($observedHash -ne $property.Value) {
        throw "GUI baseline source hash mismatch: $($property.Name)"
    }
}
$sourceManifest = Get-SourceManifest
$sourceFingerprint = Get-ManifestFingerprint $sourceManifest
$timestamp = [DateTime]::UtcNow
$buildId = $timestamp.ToString('yyyyMMddTHHmmssZ') + '-' + $sourceFingerprint.Substring(0, 8)
$buildRoot = Join-Path $OutputRoot $buildId
if (Test-Path -LiteralPath $buildRoot) { throw "Refusing to overwrite existing Agent build directory: $buildRoot" }
$metadataDirectory = Join-Path $buildRoot 'metadata'
$workPath = Join-Path $buildRoot 'work'
$distPath = Join-Path $buildRoot 'dist'
New-Item -ItemType Directory -Path $metadataDirectory -ErrorAction Stop | Out-Null
$agentInfoPath = Join-Path $metadataDirectory 'AGENT_BUILD_INFO.json'
$agentInfo = [ordered]@{
    app_name = 'Local AI Companion Agent'
    agent_version = '0.1.17-agent.1'
    package_name = $packageName
    build_id = $buildId
    build_timestamp_utc = $timestamp.ToString('o')
    workspace_path = $project
    transport = 'MCP stdio'
    protocol_version = '2024-11-05'
    gui_build_id = $guiInfo.build_id
    gui_source_fingerprint = $guiInfo.source_fingerprint
    gui_build_info_sha256 = $sourceManifest['BUILD_INFO.json']
    source_fingerprint = $sourceFingerprint
    fingerprint_algorithm = 'SHA256 of UTF-8 LF-joined ordinal-sorted relative_path:sha256 lines'
    source_input_scope = 'Root *.py; agent top-level source/spec/helper/docs/config; BUILD_INFO.json; three catalog JSON; locales files. No generated output.'
    source_file_count = $sourceManifest.Count
    source_files_sha256 = $sourceManifest
    build_runtime = $versions
}
[System.IO.File]::WriteAllText($agentInfoPath, ($agentInfo | ConvertTo-Json -Depth 10) + "`n", $utf8)
Assert-SourceUnchanged $sourceManifest 'before PyInstaller'

$oldAgentInfo = [Environment]::GetEnvironmentVariable('LOCAL_AI_COMPANION_AGENT_BUILD_INFO', 'Process')
$oldDirectory = Get-Location
try {
    [Environment]::SetEnvironmentVariable('LOCAL_AI_COMPANION_AGENT_BUILD_INFO', $agentInfoPath, 'Process')
    Set-Location -LiteralPath $project -ErrorAction Stop
    & $Python -m PyInstaller --noconfirm --clean --workpath $workPath --distpath $distPath $specPath
    if ($LASTEXITCODE -ne 0) { throw "Agent PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    Set-Location -LiteralPath $oldDirectory.Path -ErrorAction Stop
    [Environment]::SetEnvironmentVariable('LOCAL_AI_COMPANION_AGENT_BUILD_INFO', $oldAgentInfo, 'Process')
}
Assert-SourceUnchanged $sourceManifest 'after PyInstaller'

$programDirectory = Join-Path $distPath $packageName
$exePath = Join-Path $programDirectory ($packageName + '.exe')
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) { throw "Agent EXE missing: $exePath" }
$internalDirectory = Join-Path $programDirectory '_internal'
$packagedAgentInfoPath = Join-Path $internalDirectory 'AGENT_BUILD_INFO.json'
$packagedGuiInfoPath = Join-Path $internalDirectory 'BUILD_INFO.json'
$agentInfoHash = (Get-FileHash -LiteralPath $agentInfoPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
if ((Get-FileHash -LiteralPath $packagedAgentInfoPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -ne $agentInfoHash) {
    throw 'Packaged AGENT_BUILD_INFO.json differs from staged metadata'
}
if ((Get-FileHash -LiteralPath $packagedGuiInfoPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -ne $sourceManifest['BUILD_INFO.json']) {
    throw 'Packaged GUI BUILD_INFO.json differs from original metadata'
}
$packagedAgentInfo = Get-Content -LiteralPath $packagedAgentInfoPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
if ($packagedAgentInfo.build_id -ne $buildId -or $packagedAgentInfo.source_fingerprint -ne $sourceFingerprint) {
    throw 'Packaged Agent build identity differs from source build identity'
}
foreach ($entry in $sourceManifest.GetEnumerator()) {
    if ($entry.Key.StartsWith('data/') -or $entry.Key.StartsWith('locales/')) {
        $resourcePath = Join-Path $internalDirectory $entry.Key.Replace('/', '\')
        if ((Get-FileHash -LiteralPath $resourcePath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -ne $entry.Value) {
            throw "Packaged resource hash mismatch: $($entry.Key)"
        }
    }
}
$verification = [ordered]@{
    status = 'PASS'
    build_id = $buildId
    gui_build_id = $guiInfo.build_id
    source_fingerprint = $sourceFingerprint
    source_inputs_unchanged = $true
    packaged_agent_metadata_sha256 = $agentInfoHash
    packaged_gui_metadata_sha256 = $sourceManifest['BUILD_INFO.json']
    packaged_resources_verified = $true
    program_directory = $programDirectory
    exe_path = $exePath
    exe_sha256 = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    runtime_acceptance = 'UNVERIFIED: this helper does not run the EXE or call an LLM'
}
[System.IO.File]::WriteAllText((Join-Path $buildRoot 'build-verification.json'), ($verification | ConvertTo-Json -Depth 10) + "`n", $utf8)
$verification | ConvertTo-Json -Depth 10

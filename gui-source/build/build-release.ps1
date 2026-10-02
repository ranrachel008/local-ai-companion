param(
    [Parameter(Mandatory = $true)]
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$spec = Join-Path $PSScriptRoot 'Local-AI-Companion-0.1.17.spec'
$buildInfo = Join-Path $project 'BUILD_INFO.json'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}

$sourceHashes = [ordered]@{}
Get-ChildItem -LiteralPath $project -File -Filter '*.py' |
    Sort-Object Name |
    ForEach-Object {
        $sourceHashes[$_.Name] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }

$fingerprintLines = foreach ($entry in $sourceHashes.GetEnumerator()) {
    '{0}:{1}' -f $entry.Key, $entry.Value
}
$fingerprintText = $fingerprintLines -join "`n"
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $fingerprint = [BitConverter]::ToString(
        $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($fingerprintText))
    ).Replace('-', '').ToLowerInvariant()
} finally {
    $sha.Dispose()
}

$timestamp = [DateTimeOffset]::UtcNow
$buildId = '{0}-{1}' -f $timestamp.ToString('yyyyMMddTHHmmssZ'), $fingerprint.Substring(0, 8)
$work = Join-Path $PSScriptRoot "work-$buildId"
$dist = Join-Path $PSScriptRoot "dist-$buildId"
$identity = [ordered]@{
    app_name = 'Local AI Companion'
    app_version = '0.1.17'
    build_id = $buildId
    build_timestamp = $timestamp.ToString('o')
    build_timestamp_utc = $timestamp.ToString('o')
    workspace_path = $project
    source_fingerprint = $fingerprint
    fingerprint_algorithm = 'SHA256 of UTF-8 LF-joined sorted relative_path:sha256 lines'
    first_party_python_file_count = $sourceHashes.Count
    first_party_python_sha256 = $sourceHashes
    baseline_build_id = '20260930T162455Z-d5040295'
    git_state = 'LOCAL_AI_COMPANION_0_1_17_UI_HISTORY_LANGUAGE_WIRING'
    parent_build_id = '20261001T163545Z-ae244932'
}
$identityJson = $identity | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText($buildInfo, $identityJson + "`n", [Text.UTF8Encoding]::new($false))

New-Item -ItemType Directory -Path $work, $dist -Force | Out-Null

& $Python -m PyInstaller --noconfirm --clean --workpath $work --distpath $dist $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$exe = Join-Path $dist 'Local-AI-Companion-0.1.17\Local-AI-Companion-0.1.17.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Expected executable was not produced: $exe"
}
Write-Output $exe

param(
    [string]$PostgresHome = "",
    [string]$Python = "python",
    [switch]$SkipSidecar,
    [switch]$NoClean
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $RepoRoot "oculai-desktop\resources\runtime"
$PostgresTarget = Join-Path $RuntimeRoot "postgres"

if (-not $PostgresHome) {
    $candidates = Get-ChildItem "C:\Program Files\PostgreSQL" -Directory -ErrorAction SilentlyContinue |
        Sort-Object { [version]$_.Name } -Descending
    if (-not $candidates) {
        throw "PostgreSQL 16+ with pgvector was not found. Pass -PostgresHome explicitly."
    }
    $PostgresHome = $candidates[0].FullName
}

$PostgresHome = [IO.Path]::GetFullPath($PostgresHome)
$requiredSourceFiles = @(
    "bin\pg_ctl.exe",
    "bin\initdb.exe",
    "bin\postgres.exe",
    "bin\psql.exe",
    "lib\vector.dll",
    "lib\pg_trgm.dll",
    "share\extension\vector.control",
    "share\extension\pg_trgm.control"
)
foreach ($relative in $requiredSourceFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $PostgresHome $relative))) {
        throw "PostgreSQL runtime is incomplete: missing $relative under $PostgresHome"
    }
}

$resolvedRuntime = [IO.Path]::GetFullPath($RuntimeRoot)
$expectedPrefix = [IO.Path]::GetFullPath((Join-Path $RepoRoot "oculai-desktop\resources")) + [IO.Path]::DirectorySeparatorChar
if (-not $resolvedRuntime.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write runtime outside the desktop resources directory: $resolvedRuntime"
}

if (-not $NoClean -and (Test-Path -LiteralPath $RuntimeRoot)) {
    Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $PostgresTarget -Force | Out-Null

foreach ($directory in @("bin", "lib", "share")) {
    Copy-Item -LiteralPath (Join-Path $PostgresHome $directory) -Destination $PostgresTarget -Recurse -Force
}
foreach ($license in @(
    "COPYRIGHT",
    "LICENSE",
    "server_license.txt",
    "commandlinetools_3rd_party_licenses.txt"
)) {
    $source = Join-Path $PostgresHome $license
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $PostgresTarget -Force
    }
}
$licenseTarget = Join-Path $RuntimeRoot "licenses"
New-Item -ItemType Directory -Path $licenseTarget -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $RepoRoot "licenses\pgvector-LICENSE") `
    -Destination $licenseTarget -Force

if (-not $SkipSidecar) {
    & $Python (Join-Path $PSScriptRoot "build_python_sidecar.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Python sidecar build failed with exit code $LASTEXITCODE"
    }
}

& $Python (Join-Path $PSScriptRoot "verify_runtime_bundle.py") --root $RuntimeRoot --write-manifest --smoke-sidecar
if ($LASTEXITCODE -ne 0) {
    throw "Runtime verification failed with exit code $LASTEXITCODE"
}

Write-Output "Validated Windows runtime at $RuntimeRoot"

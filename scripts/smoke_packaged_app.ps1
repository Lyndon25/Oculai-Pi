[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$App,
    [switch]$CloseDuringStartup,
    [ValidateRange(30, 300)]
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$AppRoot = if ([System.IO.Path]::IsPathRooted($App)) {
    [System.IO.Path]::GetFullPath($App)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $App))
}
$Executable = Join-Path $AppRoot "Oculai.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Packaged Oculai executable was not found: $Executable"
}

$WorkRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "work\packaged-app-smoke"))
$CaseRoot = [System.IO.Path]::GetFullPath((Join-Path $WorkRoot ([guid]::NewGuid().ToString())))
if (-not $CaseRoot.StartsWith(
    $WorkRoot + [System.IO.Path]::DirectorySeparatorChar,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Unsafe packaged app smoke directory: $CaseRoot"
}
New-Item -ItemType Directory -Path $CaseRoot -Force | Out-Null

$Profile = Join-Path $CaseRoot "profile"
$StdoutLog = Join-Path $CaseRoot "stdout.log"
$StderrLog = Join-Path $CaseRoot "stderr.log"
$RuntimeRoot = Join-Path $AppRoot "resources\runtime"
$DataDir = Join-Path $Profile "postgres\data"
$PgCtl = Join-Path $RuntimeRoot "postgres\bin\pg_ctl.exe"
$PgReady = Join-Path $RuntimeRoot "postgres\bin\pg_isready.exe"
$Process = $null
$ReadyObserved = $false
$Succeeded = $false
$OldExitMs = $env:OCULAI_SMOKE_EXIT_MS
$OldEarlyCloseMs = $env:OCULAI_SMOKE_CLOSE_DURING_START_MS

function Get-PackagedProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith(
            $AppRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    })
}

try {
    New-Item -ItemType Directory -Path $Profile -Force | Out-Null
    if ($CloseDuringStartup) {
        Remove-Item Env:OCULAI_SMOKE_EXIT_MS -ErrorAction SilentlyContinue
        $env:OCULAI_SMOKE_CLOSE_DURING_START_MS = "1000"
    } else {
        $env:OCULAI_SMOKE_EXIT_MS = "10000"
        Remove-Item Env:OCULAI_SMOKE_CLOSE_DURING_START_MS -ErrorAction SilentlyContinue
    }

    $LaunchCount = if ($CloseDuringStartup) { 1 } else { 2 }
    for ($Launch = 1; $Launch -le $LaunchCount; $Launch++) {
        $StdoutLog = Join-Path $CaseRoot "stdout-$Launch.log"
        $StderrLog = Join-Path $CaseRoot "stderr-$Launch.log"
        $ReadyObserved = $false
        $Process = Start-Process `
            -FilePath $Executable `
            -ArgumentList @("--user-data-dir=$Profile", "--disable-gpu") `
            -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StdoutLog `
            -RedirectStandardError $StderrLog

        $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        do {
            Start-Sleep -Milliseconds 250
            $Process.Refresh()
            $PackagedProcesses = @(Get-PackagedProcesses)
            $Postgres = $PackagedProcesses |
                Where-Object { $_.Name -eq "postgres.exe" -and $_.CommandLine -match "\s-D\s" } |
                Select-Object -First 1
            $Sidecar = $PackagedProcesses |
                Where-Object { $_.Name -eq "oculai-sidecar.exe" } |
                Select-Object -First 1
            $RendererLoaded = (Test-Path -LiteralPath $StdoutLog) -and [bool](
                Select-String -LiteralPath $StdoutLog -Pattern '"rootChildren":[1-9]' -Quiet
            )
            $DatabaseConnected = (Test-Path -LiteralPath $StdoutLog) -and [bool](
                Select-String -LiteralPath $StdoutLog `
                    -Pattern '\[system:status\].*"db":"connected"' -Quiet
            )
            $BackendReady = (Test-Path -LiteralPath $StdoutLog) -and [bool](
                Select-String -LiteralPath $StdoutLog -Pattern "Oculai Desktop backend ready" -Quiet
            )

            if (
                -not $ReadyObserved -and $Postgres -and $Sidecar -and $RendererLoaded -and
                $DatabaseConnected -and $BackendReady -and
                (Test-Path -LiteralPath (Join-Path $DataDir "PG_VERSION"))
            ) {
                if ($Postgres.CommandLine -notmatch '-p\s+(\d+)') {
                    throw "Could not read packaged PostgreSQL port from: $($Postgres.CommandLine)"
                }
                $Port = [int]$Matches[1]
                & $PgReady -h 127.0.0.1 -p $Port -t 5 | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-Host (
                        "Launch $Launch ready: main=$($Process.Id), postgres=$($Postgres.ProcessId), " +
                        "sidecar=$($Sidecar.ProcessId), port=$Port"
                    )
                    $ReadyObserved = $true
                }
            }

            if ($Process.HasExited) { break }
            if ((Get-Date) -gt $Deadline) {
                throw "Timed out waiting for packaged app lifecycle smoke launch $Launch"
            }
        } while ($true)

        $Process.WaitForExit()
        $Process.Refresh()
        if (-not $ReadyObserved -and -not $CloseDuringStartup) {
            throw "Packaged app launch $Launch exited without live readiness evidence"
        }
        if ($null -ne $Process.ExitCode -and $Process.ExitCode -ne 0) {
            throw "Packaged app launch $Launch exited with code $($Process.ExitCode)"
        }

        $RequiredLogPatterns = @(
            '\[system:status\].*"db":"connected"',
            'Python sidecar ready: 43 tools',
            'Oculai Desktop backend ready',
            'Shutdown complete'
        )
        if (-not $CloseDuringStartup) {
            $RequiredLogPatterns = @('"rootChildren":[1-9]') + $RequiredLogPatterns
        }
        if ($Launch -eq 2) {
            $RequiredLogPatterns += 'All 2 migrations already applied'
        }
        foreach ($Pattern in $RequiredLogPatterns) {
            if (-not (Select-String -LiteralPath $StdoutLog -Pattern $Pattern -Quiet)) {
                throw "Packaged app launch $Launch log is missing required evidence: $Pattern"
            }
        }
        if (Select-String -LiteralPath $StdoutLog,$StderrLog `
            -Pattern 'db":"error|renderer:load-failed|PostgreSQL failed|sidecar failed|ProactorReadPipeTransport|Traceback' `
            -Quiet
        ) {
            throw "Packaged app launch $Launch logs contain a backend, renderer, or protocol failure"
        }

        $Remaining = @(Get-PackagedProcesses)
        if ($Remaining.Count -ne 0) {
            throw "Packaged app launch $Launch left $($Remaining.Count) process(es)"
        }
        Write-Host "OK: packaged app launch $Launch/$LaunchCount exited with zero residual processes"
    }
    $Mode = if ($CloseDuringStartup) { "startup-race" } else { "clean-start-and-restart" }
    Write-Host "OK: packaged app $Mode lifecycle smoke passed"
    $Succeeded = $true
} finally {
    if ($null -eq $OldExitMs) {
        Remove-Item Env:OCULAI_SMOKE_EXIT_MS -ErrorAction SilentlyContinue
    } else {
        $env:OCULAI_SMOKE_EXIT_MS = $OldExitMs
    }
    if ($null -eq $OldEarlyCloseMs) {
        Remove-Item Env:OCULAI_SMOKE_CLOSE_DURING_START_MS -ErrorAction SilentlyContinue
    } else {
        $env:OCULAI_SMOKE_CLOSE_DURING_START_MS = $OldEarlyCloseMs
    }

    if ($Process) {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            & taskkill /PID $Process.Id /T /F | Out-Null
        }
    }
    $Remaining = @(Get-PackagedProcesses)
    if (
        ($Remaining | Where-Object Name -eq "postgres.exe") -and
        (Test-Path -LiteralPath (Join-Path $DataDir "PG_VERSION"))
    ) {
        & $PgCtl stop -D $DataDir -m immediate -w | Out-Null
    }
    Get-PackagedProcesses | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

    if ($Succeeded -and $CaseRoot.StartsWith(
        $WorkRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        Remove-Item -LiteralPath $CaseRoot -Recurse -Force
    } elseif (-not $Succeeded) {
        Write-Warning "Packaged app smoke logs preserved at $CaseRoot"
        foreach ($Log in (Get-ChildItem -LiteralPath $CaseRoot -Filter "*.log" -File -ErrorAction SilentlyContinue)) {
            Write-Host "--- $($Log.Name) ---"
            Get-Content -LiteralPath $Log.FullName -Tail 200
        }
    }
}

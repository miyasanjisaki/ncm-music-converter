[CmdletBinding()]
param(
    [string]$BootstrapPython = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$VenvRoot = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$ReleaseRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "release"))
$AppName = "NCM" + (-join [char[]](0x97F3, 0x4E50, 0x8F6C, 0x6362, 0x5668))
$BuildRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "build"))
$SmokeRoot = [System.IO.Path]::GetFullPath((Join-Path $BuildRoot "packaged-smoke"))

if ([string]::IsNullOrWhiteSpace($BootstrapPython)) {
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $PythonCommand) {
        $PythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    }
    if ($null -eq $PythonCommand) {
        throw "Python was not found on PATH. Pass -BootstrapPython with a Python executable path."
    }
    $BootstrapPython = $PythonCommand.Source
}

if (-not $ReleaseRoot.StartsWith($ProjectRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to clean a release path outside the project: $ReleaseRoot"
}
if (-not $SmokeRoot.StartsWith($BuildRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a smoke-test path outside the build directory: $SmokeRoot"
}
if (-not (Test-Path -LiteralPath $BootstrapPython -PathType Leaf)) {
    throw "Python not found: $BootstrapPython"
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    & $BootstrapPython -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not create the build virtual environment." }
}
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install the build dependencies." }

if (-not $SkipTests) {
    & $VenvPython -m compileall -q (Join-Path $ProjectRoot "src") (Join-Path $ProjectRoot "tests")
    if ($LASTEXITCODE -ne 0) { throw "Python compilation check failed." }
    & $VenvPython -m unittest discover -s (Join-Path $ProjectRoot "tests") -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
}

if (Test-Path -LiteralPath $ReleaseRoot) {
    Remove-Item -LiteralPath $ReleaseRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null

& $VenvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name $AppName `
    --paths (Join-Path $ProjectRoot "src") `
    --version-file (Join-Path $ProjectRoot "build_support\version_info.txt") `
    --manifest (Join-Path $ProjectRoot "build_support\app.manifest") `
    --distpath $ReleaseRoot `
    --workpath (Join-Path $ProjectRoot "build\pyinstaller") `
    --specpath (Join-Path $ProjectRoot "build") `
    (Join-Path $ProjectRoot "src\ncm_gui.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$AppFolder = Join-Path $ReleaseRoot $AppName
$AppExe = Join-Path $AppFolder ($AppName + ".exe")
$Smoke = Start-Process -FilePath $AppExe -ArgumentList "--smoke-test" -PassThru -WindowStyle Hidden -Wait
if ($Smoke.ExitCode -ne 0) { throw "Packaged GUI smoke test failed with exit code $($Smoke.ExitCode)." }
$SmokeInput = Join-Path $SmokeRoot "packaged-smoke.ncm"
$SmokeOutput = Join-Path $SmokeRoot "output"
if (Test-Path -LiteralPath $SmokeRoot) {
    Remove-Item -LiteralPath $SmokeRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $SmokeOutput | Out-Null
& $VenvPython (Join-Path $ProjectRoot "tests\packaged_smoke.py") create $SmokeInput
if ($LASTEXITCODE -ne 0) { throw "Could not create the packaged conversion fixture." }
$QuotedSmokeArguments = '--self-test-convert "{0}" "{1}"' -f $SmokeInput.Replace('"', '\"'), $SmokeOutput.Replace('"', '\"')
$ConversionSmoke = Start-Process -FilePath $AppExe -ArgumentList $QuotedSmokeArguments -PassThru -WindowStyle Hidden -Wait
if ($ConversionSmoke.ExitCode -ne 0) { throw "Packaged conversion smoke test failed with exit code $($ConversionSmoke.ExitCode)." }
$SmokeResult = Join-Path $SmokeOutput "packaged-smoke.flac"
& $VenvPython (Join-Path $ProjectRoot "tests\packaged_smoke.py") verify $SmokeResult
if ($LASTEXITCODE -ne 0) { throw "Packaged conversion output verification failed." }
$SmokeLogFolder = [System.IO.Path]::GetFullPath((Join-Path $AppFolder "logs"))
if (Test-Path -LiteralPath $SmokeLogFolder) {
    if (-not $SmokeLogFolder.StartsWith([System.IO.Path]::GetFullPath($AppFolder) + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean smoke-test logs outside the app folder: $SmokeLogFolder"
    }
    Remove-Item -LiteralPath $SmokeLogFolder -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination (Join-Path $AppFolder "README.md")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "LICENSE") -Destination (Join-Path $AppFolder "LICENSE.txt")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD-PARTY-NOTICES.txt") -Destination (Join-Path $AppFolder "THIRD-PARTY-NOTICES.txt")

$LicenseFolder = Join-Path $AppFolder "licenses"
New-Item -ItemType Directory -Force -Path $LicenseFolder | Out-Null
$SitePackages = Join-Path $VenvRoot "Lib\site-packages"
Copy-Item -LiteralPath (Join-Path $SitePackages "mutagen-1.48.1.dist-info\licenses\COPYING") -Destination (Join-Path $LicenseFolder "Mutagen-GPL-2.0-or-later.txt")
Copy-Item -LiteralPath (Join-Path $SitePackages "pycryptodome-3.23.0.dist-info\LICENSE.rst") -Destination (Join-Path $LicenseFolder "PyCryptodome-BSD-Public-Domain.rst")
Copy-Item -LiteralPath (Join-Path $SitePackages "pycryptodome-3.23.0.dist-info\AUTHORS.rst") -Destination (Join-Path $LicenseFolder "PyCryptodome-AUTHORS.rst")
Copy-Item -LiteralPath (Join-Path $SitePackages "tkinterdnd2-0.6.2.dist-info\licenses\LICENSE") -Destination (Join-Path $LicenseFolder "tkinterdnd2-MIT.txt")
Copy-Item -LiteralPath (Join-Path $SitePackages "pyinstaller-6.21.0.dist-info\licenses\COPYING.txt") -Destination (Join-Path $LicenseFolder "PyInstaller-GPL-with-bootloader-exception.txt")
$BasePrefix = (& $VenvPython -c "import sys; print(sys.base_prefix)").Trim()
$PythonLicense = Join-Path $BasePrefix "LICENSE.txt"
if (Test-Path -LiteralPath $PythonLicense -PathType Leaf) {
    Copy-Item -LiteralPath $PythonLicense -Destination (Join-Path $LicenseFolder "Python-PSF-LICENSE.txt")
}

$SourceFolder = Join-Path $AppFolder "source"
New-Item -ItemType Directory -Force -Path $SourceFolder | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot "src") -Destination $SourceFolder -Recurse
Copy-Item -LiteralPath (Join-Path $ProjectRoot "tests") -Destination $SourceFolder -Recurse
Copy-Item -LiteralPath (Join-Path $ProjectRoot "build_support") -Destination $SourceFolder -Recurse
Copy-Item -LiteralPath (Join-Path $ProjectRoot "build.ps1") -Destination $SourceFolder
Copy-Item -LiteralPath (Join-Path $ProjectRoot "requirements.txt") -Destination $SourceFolder
Copy-Item -LiteralPath (Join-Path $ProjectRoot "requirements-build.txt") -Destination $SourceFolder
Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination $SourceFolder
Copy-Item -LiteralPath (Join-Path $ProjectRoot "LICENSE") -Destination $SourceFolder
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD-PARTY-NOTICES.txt") -Destination $SourceFolder
$MutagenSource = Join-Path $SourceFolder "third_party\mutagen-1.48.1"
New-Item -ItemType Directory -Force -Path $MutagenSource | Out-Null
Copy-Item -LiteralPath (Join-Path $SitePackages "mutagen") -Destination $MutagenSource -Recurse
Copy-Item -LiteralPath (Join-Path $SitePackages "mutagen-1.48.1.dist-info\licenses\COPYING") -Destination $MutagenSource
$CacheFolders = Get-ChildItem -LiteralPath $SourceFolder -Directory -Recurse -Force | Where-Object Name -eq "__pycache__"
foreach ($CacheFolder in $CacheFolders) {
    $ResolvedCache = [System.IO.Path]::GetFullPath($CacheFolder.FullName)
    if (-not $ResolvedCache.StartsWith([System.IO.Path]::GetFullPath($SourceFolder) + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove cache outside source output: $ResolvedCache"
    }
    Remove-Item -LiteralPath $ResolvedCache -Recurse -Force
}

$Checksums = Get-ChildItem -LiteralPath $AppFolder -Recurse -File |
    Sort-Object FullName |
    ForEach-Object {
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $relative = $_.FullName.Substring($AppFolder.Length).TrimStart('\').Replace('\', '/')
        "$hash  $relative"
    }
$ChecksumText = ($Checksums -join "`n") + "`n"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $AppFolder "SHA256SUMS.txt"), $ChecksumText, $Utf8NoBom)

$ZipPath = Join-Path $ReleaseRoot ($AppName + "-v1.0.0-Windows-x64.zip")
Compress-Archive -LiteralPath $AppFolder -DestinationPath $ZipPath -CompressionLevel Optimal
$ZipSmokeRoot = [System.IO.Path]::GetFullPath((Join-Path $BuildRoot "zip-smoke"))
if (-not $ZipSmokeRoot.StartsWith($BuildRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use a ZIP smoke-test path outside the build directory: $ZipSmokeRoot"
}
if (Test-Path -LiteralPath $ZipSmokeRoot) {
    Remove-Item -LiteralPath $ZipSmokeRoot -Recurse -Force
}
Expand-Archive -LiteralPath $ZipPath -DestinationPath $ZipSmokeRoot
$ExtractedExe = Join-Path (Join-Path $ZipSmokeRoot $AppName) ($AppName + ".exe")
$ExtractedOutput = Join-Path $SmokeRoot "zip-output"
New-Item -ItemType Directory -Force -Path $ExtractedOutput | Out-Null
$QuotedZipArguments = '--self-test-convert "{0}" "{1}"' -f $SmokeInput.Replace('"', '\"'), $ExtractedOutput.Replace('"', '\"')
$ZipSmoke = Start-Process -FilePath $ExtractedExe -ArgumentList $QuotedZipArguments -PassThru -WindowStyle Hidden -Wait
if ($ZipSmoke.ExitCode -ne 0) { throw "Extracted ZIP conversion smoke test failed with exit code $($ZipSmoke.ExitCode)." }
& $VenvPython (Join-Path $ProjectRoot "tests\packaged_smoke.py") verify (Join-Path $ExtractedOutput "packaged-smoke.flac")
if ($LASTEXITCODE -ne 0) { throw "Extracted ZIP conversion output verification failed." }
Remove-Item -LiteralPath $ZipSmokeRoot -Recurse -Force
Remove-Item -LiteralPath $SmokeRoot -Recurse -Force
Write-Host "Build complete: $ZipPath"

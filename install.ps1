# BudsTrack setup: installs what is missing (Python, opentrack), creates the shortcuts
# and starts BudsTrack in the background. Run it through Kur.bat.
#   -Uninstall   remove the shortcuts again
#   -NoPrompt    do not ask questions (no autostart shortcut)
#   -NoLaunch    do not start BudsTrack at the end
param([switch]$Uninstall, [switch]$NoPrompt, [switch]$NoLaunch)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$tr = (Get-UICulture).TwoLetterISOLanguageName -eq 'tr'
function Say($turkish, $english) { if ($tr) { Write-Host $turkish } else { Write-Host $english } }

$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$startup = [Environment]::GetFolderPath('Startup')
$links = @("$desktop\BudsTrack.lnk", "$programs\BudsTrack.lnk", "$startup\BudsTrack.lnk")

if ($Uninstall) {
    $links | Where-Object { Test-Path $_ } | ForEach-Object { Remove-Item $_ -Force }
    Say 'Kisayollar kaldirildi. Klasoru silerek kaldirmayi tamamlayabilirsiniz.' 'Shortcuts removed. Delete this folder to finish.'
    exit 0
}

Say '== BudsTrack kurulumu ==' '== BudsTrack setup =='

# ---- 1. Python (with tkinter) -------------------------------------------------
function Find-Python {
    foreach ($cmd in @('py -3', 'python')) {
        try {
            $exe = Invoke-Expression "$cmd -c `"import sys, tkinter; print(sys.executable)`"" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe.Trim() }
        } catch { }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Say '[1/3] Python bulunamadi, kuruluyor...' '[1/3] Python not found, installing...'
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Say 'winget yok. Lutfen https://www.python.org/downloads/ adresinden Python kurup Kur.bat i yeniden calistirin.' `
            'winget is missing. Install Python from https://www.python.org/downloads/ and run Kur.bat again.'
        exit 1
    }
    winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $python = Find-Python
    if (-not $python) {
        Say 'Python kuruldu ama bulunamadi. Bilgisayari yeniden baslatip Kur.bat i tekrar calistirin.' `
            'Python was installed but cannot be found yet. Restart the PC and run Kur.bat again.'
        exit 1
    }
} else {
    Say "[1/3] Python hazir: $python" "[1/3] Python found: $python"
}
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

# ---- 2. opentrack (its TrackIR / freetrack client files are what games load) ----
function Find-OpenTrack {
    $dirs = @("${env:ProgramFiles(x86)}\opentrack", "$env:ProgramFiles\opentrack", "$env:LOCALAPPDATA\Programs\opentrack")
    $dirs | Where-Object { $_ -and (Test-Path "$_\modules\NPClient64.dll") } | Select-Object -First 1
}

if (Find-OpenTrack) {
    Say '[2/3] opentrack hazir.' '[2/3] opentrack found.'
} else {
    Say '[2/3] opentrack indiriliyor (yaklasik 190 MB)...' '[2/3] Downloading opentrack (about 190 MB)...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $release = Invoke-RestMethod 'https://api.github.com/repos/opentrack/opentrack/releases/latest' -Headers @{ 'User-Agent' = 'BudsTrack-setup' }
    $asset = $release.assets | Where-Object { $_.name -like '*win32-setup.exe' } | Select-Object -First 1
    if (-not $asset) { throw 'opentrack setup file not found in the latest release' }
    $setup = Join-Path $env:TEMP $asset.name
    & curl.exe -L --fail --progress-bar -o $setup $asset.browser_download_url
    if ($LASTEXITCODE -ne 0 -or (Get-Item $setup).Length -ne $asset.size) { throw 'opentrack download failed' }
    Say 'opentrack kuruluyor; Windows izin isterse Evet deyin...' 'Installing opentrack; answer Yes if Windows asks...'
    Start-Process $setup -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait
    Remove-Item $setup -Force -ErrorAction SilentlyContinue
    if (-not (Find-OpenTrack)) { throw 'opentrack installation did not complete' }
}

# ---- 3. Shortcuts ---------------------------------------------------------------
Say '[3/3] Kisayollar olusturuluyor...' '[3/3] Creating shortcuts...'
& $python -c "import sys; sys.path.insert(0, r'$here'); import budstrack; budstrack.ensure_icon()"
$shell = New-Object -ComObject WScript.Shell
function New-Link($path) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = "`"$here\budstrack.py`" --background"
    $link.WorkingDirectory = $here
    $link.Description = 'BudsTrack - Galaxy Buds head tracking'
    if (Test-Path "$here\budstrack.ico") { $link.IconLocation = "$here\budstrack.ico" }
    $link.Save()
}
New-Link $links[0]
New-Link $links[1]

if (-not $NoPrompt) {
    $answer = if ($tr) { Read-Host 'BudsTrack Windows acilisinda kendiliginden baslasin mi? (E/H)' }
              else { Read-Host 'Start BudsTrack automatically with Windows? (Y/N)' }
    if ($answer -match '^[eEyY]') { New-Link $links[2] }
}

Say '' ''
Say 'Kurulum tamam. Masaustundeki BudsTrack kisayolunu kullanin.' 'Setup finished. Use the BudsTrack shortcut on your desktop.'
Say 'BudsTrack arka planda calisir; saatin yanindaki mavi simgeye tiklayinca penceresi acilir.' `
    'BudsTrack runs in the background; click the blue icon next to the clock to open its window.'
Say 'Oyunu BudsTrack calisirken baslatin.' 'Start your game while BudsTrack is running.'

if (-not $NoLaunch) {
    Start-Process $pythonw -ArgumentList "`"$here\budstrack.py`"" -WorkingDirectory $here
}

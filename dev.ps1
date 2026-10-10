<#
.SYNOPSIS
    Một script cho cả dự án: firmware logger, công cụ DDSU666, broker MQTT để test.

.DESCRIPTION
    Chạy qua dev.bat:  dev.bat <lệnh> [-Port COMx] [tùy chọn]
    dev.bat help in danh sách lệnh và tùy chọn.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",
    [string]$Port = "",
    [int]$FlashBaud = 460800,
    [switch]$NoMonitor,
    [int]$BrokerPort = 1883,
    [string]$IdfPath = ""
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = $PSScriptRoot
$LoggerFw = Join-Path $Root "firmware"
$BridgeFw = Join-Path $Root "tools\ddsu666\firmware"
$Gui = Join-Path $Root "tools\ddsu666\gui"
$Broker = Join-Path $Root "tools\local-broker"

$Help = @"
Cách dùng:  dev.bat <lệnh> [-Port COMx] [tùy chọn]

Firmware logger (firmware\)
  build          build firmware logger
  flash          build, nạp logger rồi mở log (thêm -NoMonitor để không mở log)
  monitor        xem log của board (Ctrl+] để thoát)
  config         cấu hình logger: Wi-Fi, MQTT, mã tòa, danh sách đồng hồ (menuconfig)
  clean          xóa thư mục build của logger và firmware cầu nối

Công cụ đồng hồ DDSU666 (tools\ddsu666\)
  bridge         build, nạp firmware cầu nối RS485 rồi mở app, tự kết nối
  bridge-build   chỉ build firmware cầu nối
  app            chỉ mở app (board đã có firmware cầu nối)

Broker MQTT để test (tools\local-broker\)
  broker         chạy broker trên máy này (Ctrl+C để dừng)

Khác
  test           chạy toàn bộ test (app DDSU666 + broker)
  ports          liệt kê cổng COM
  help           in trang này

Tùy chọn
  -Port COM16       cổng của board; bỏ trống thì tự tìm cổng USB-serial
  -FlashBaud 115200 tốc độ nạp (mặc định 460800; giảm nếu nạp lỗi)
  -NoMonitor        flash xong không mở log
  -BrokerPort 1884  cổng broker (mặc định 1883)
  -IdfPath C:\...   thư mục ESP-IDF (mặc định tìm C:\Espressif\frameworks\esp-idf-v5.*)

Ví dụ
  dev.bat config
  dev.bat flash -Port COM16
  dev.bat bridge -Port COM16
  dev.bat broker
"@

$Commands = @("help", "build", "flash", "monitor", "config", "clean", "bridge", "bridge-build", "app", "broker",
              "test", "ports")

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Note([string]$Text) {
    Write-Host "  ! $Text" -ForegroundColor Yellow
}

function Stop-WithError([string]$Text) {
    Write-Host ""
    Write-Host "LỖI: $Text" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- COM ports

function Get-SerialPorts {
    Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '\(COM\d+\)' } |
        ForEach-Object {
            [pscustomobject]@{ Port = ($_.Name -replace '^.*\((COM\d+)\).*$', '$1'); Name = $_.Name }
        } |
        Sort-Object { [int]($_.Port -replace '\D', '') }
}

function Resolve-Port {
    if ($Port) { return $Port.ToUpper() }
    $ports = @(Get-SerialPorts)
    if ($ports.Count -eq 0) {
        Stop-WithError "Không thấy cổng COM nào. Cắm ESP32 vào USB (cần driver CH340/CP210x) rồi chạy lại."
    }
    $usb = @($ports | Where-Object { $_.Name -match 'CH34|CH91|CP210|USB.?SERIAL|USB.?UART|FTDI|Silicon Labs|JTAG' })
    if ($usb.Count -eq 1) { $pick = $usb[0] }
    elseif ($ports.Count -eq 1) { $pick = $ports[0] }
    else {
        Write-Host "Có nhiều cổng COM:"
        for ($i = 0; $i -lt $ports.Count; $i++) { Write-Host ("  [{0}] {1}" -f ($i + 1), $ports[$i].Name) }
        $answer = Read-Host "Chọn số thứ tự cổng của ESP32"
        $index = 0
        if (-not [int]::TryParse($answer, [ref]$index) -or $index -lt 1 -or $index -gt $ports.Count) {
            Stop-WithError "Lựa chọn không hợp lệ: '$answer'"
        }
        $pick = $ports[$index - 1]
    }
    Write-Host "Dùng cổng $($pick.Name)"
    return $pick.Port
}

# ---------------------------------------------------------------- Python (app DDSU666)

function Find-Python {
    # Resolve before activating ESP-IDF: its export script puts its own venv Python first in PATH.
    $candidates = @()
    foreach ($command in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($command.Source -and $command.Source -notmatch 'WindowsApps') { $candidates += $command.Source }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $fromLauncher = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $fromLauncher) { $candidates += $fromLauncher.Trim() }
    }
    foreach ($exe in $candidates) {
        & $exe -c "import tkinter" 2>$null
        if ($LASTEXITCODE -eq 0) { return $exe }
    }
    Stop-WithError "Không tìm thấy Python 3 có Tkinter. Cài Python từ https://www.python.org (chọn 'tcl/tk and IDLE')."
}

function Install-AppRequirements([string]$Python) {
    & $Python -c "import serial" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Step "Cài thư viện pyserial"
        & $Python -m pip install -r (Join-Path $Gui "requirements.txt")
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Không cài được pyserial (kiểm tra mạng / pip)." }
    }
}

function Start-App([string]$Python, [string[]]$AppArgs) {
    Install-AppRequirements $Python
    Write-Step "Mở app DDSU666 $($AppArgs -join ' ')"
    Push-Location $Gui
    try { & $Python (Join-Path $Gui "ddsu666_gui.py") @AppArgs } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Stop-WithError "App thoát với mã lỗi $LASTEXITCODE." }
}

# ---------------------------------------------------------------- Node.js (broker)

function Find-Node {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Stop-WithError "Không tìm thấy Node.js. Cài bản 20 trở lên từ https://nodejs.org rồi chạy lại."
    }
    $major = [int]((& node -v) -replace '^v(\d+).*$', '$1')
    if ($major -lt 20) { Stop-WithError "Broker cần Node.js 20 trở lên (máy đang có $(& node -v))." }
}

function Install-BrokerPackages {
    if (-not (Test-Path (Join-Path $Broker "node_modules"))) {
        Write-Step "Cài thư viện cho broker (npm install)"
        Push-Location $Broker
        try { & npm.cmd install --no-fund --no-audit } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { Stop-WithError "npm install thất bại (kiểm tra mạng)." }
    }
}

function Wait-TcpPort([int]$Number, [double]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object System.Net.Sockets.TcpClient
        try { $client.Connect("127.0.0.1", $Number); return $true } catch { Start-Sleep -Milliseconds 100 }
        finally { $client.Dispose() }
    }
    return $false
}

# ---------------------------------------------------------------- ESP-IDF

function Find-IdfExport {
    $roots = @()
    if ($IdfPath) { $roots += $IdfPath }
    elseif ($env:IDF_PATH) { $roots += $env:IDF_PATH }
    else {
        $frameworks = "C:\Espressif\frameworks"
        if (Test-Path $frameworks) {
            $roots += Get-ChildItem $frameworks -Directory -Filter "esp-idf-v5*" |
                Sort-Object Name -Descending | ForEach-Object { $_.FullName }
        }
    }
    foreach ($candidate in $roots) {
        $export = Join-Path $candidate "export.ps1"
        if (Test-Path $export) { return $export }
    }
    Stop-WithError ("Không tìm thấy ESP-IDF 5.x. Mở 'ESP-IDF 5.3 PowerShell' rồi chạy lại, " +
                    "hoặc truyền -IdfPath C:\duong\dan\esp-idf.")
}

function Invoke-Idf([string]$Project, [string[]]$IdfArgs) {
    idf.py -C $Project @IdfArgs
    if ($LASTEXITCODE -ne 0) { Stop-WithError "idf.py $($IdfArgs -join ' ') thất bại (mã $LASTEXITCODE)." }
}

function Read-Sdkconfig([string]$Name) {
    $file = Join-Path $LoggerFw "sdkconfig"
    if (-not (Test-Path $file)) { return $null }
    $line = Select-String -Path $file -Pattern "^CONFIG_$Name=(.*)$" | Select-Object -First 1
    if (-not $line) { return $null }
    return $line.Matches[0].Groups[1].Value.Trim('"')
}

function Test-RoomPairs([string]$Text) {
    # Same rules as meter_parse_rooms(): "e:w[,e:w]...", addresses 1-247, all different, at most 16 rooms.
    if ($Text -notmatch '^\d{1,3}:\d{1,3}(,\d{1,3}:\d{1,3})*$') { return $false }
    $ids = @($Text -split '[:,]' | ForEach-Object { [int]$_ })
    if ($ids.Count / 2 -gt 16) { return $false }
    if (@($ids | Where-Object { $_ -lt 1 -or $_ -gt 247 }).Count -gt 0) { return $false }
    return @($ids | Sort-Object -Unique).Count -eq $ids.Count
}

function Test-LoggerConfig {
    # $false when the logger would stop right after boot: it checks the room list before anything else.
    $rooms = Read-Sdkconfig "LOGGER_ROOMS"
    $uri = Read-Sdkconfig "LOGGER_MQTT_URI"
    $ssid = Read-Sdkconfig "LOGGER_WIFI_SSID"
    $ok = $true
    if (-not $rooms) {
        Write-Note "Chưa đặt 'Room meter addresses': firmware sẽ dừng ngay khi khởi động."
        $ok = $false
    } elseif (-not (Test-RoomPairs $rooms)) {
        Write-Note ("'Room meter addresses' = `"$rooms`" không hợp lệ: cần địa chỉ Modbus dạng điện:nước, " +
                    "ví dụ 1:2 hoặc 1:2,3:4 (số 1-247, không trùng nhau, tối đa 16 phòng).")
        $ok = $false
    }
    if (-not $uri) { Write-Note "Chưa đặt 'MQTT broker URI': logger chạy offline, không gửi dữ liệu (Wi-Fi cũng không bật)." }
    elseif (-not $ssid) { Write-Note "Có MQTT URI nhưng chưa đặt Wi-Fi SSID: logger sẽ không kết nối được." }
    if ($uri -and (Read-Sdkconfig "LOGGER_MQTT_QOS") -eq "0") {
        Write-Note "MQTT QoS = 0: bản ghi bị xóa khỏi hàng đợi ngay khi gửi, không chờ broker xác nhận (dùng QoS 1)."
    }
    if ((Read-Sdkconfig "LOGGER_FAKE_ELECTRIC") -eq "y") {
        Write-Note "Đang BẬT dữ liệu điện GIẢ LẬP (status `"simulated`"): chỉ để test, tắt trước khi lắp thật."
    }
    if (-not $ok) { Write-Note "Sửa bằng: dev.bat config (menu Water logger)" }
    return $ok
}

# ---------------------------------------------------------------- main

if ($Commands -notcontains $Command) {
    Write-Host $Help
    Stop-WithError "Không có lệnh '$Command'."
}

if ($Command -eq "help") { Write-Host $Help; exit 0 }

if ($Command -eq "ports") {
    $ports = @(Get-SerialPorts)
    if ($ports.Count -eq 0) { Write-Host "Không có cổng COM nào." } else { $ports | ForEach-Object { Write-Host $_.Name } }
    exit 0
}

$Python = $null
if ($Command -in @("bridge", "app", "test")) { $Python = Find-Python }
if ($Command -in @("broker", "test")) { Find-Node }

if ($Command -in @("build", "flash", "monitor", "config", "clean", "bridge", "bridge-build")) {
    if (Get-Command idf.py -ErrorAction SilentlyContinue) {
        Write-Host "Dùng ESP-IDF đang mở: $env:IDF_PATH"
    } else {
        $export = Find-IdfExport
        if (-not $env:IDF_TOOLS_PATH -and (Test-Path "C:\Espressif\python_env")) { $env:IDF_TOOLS_PATH = "C:\Espressif" }
        $exportLog = Join-Path $env:TEMP "esp32-water-logger_idf_export.log"
        Write-Step "Kích hoạt ESP-IDF: $(Split-Path $export -Parent) (log: $exportLog)"
        # Dot-sourced at script scope so that the idf.py function it defines stays available.
        . $export *> $exportLog
        if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) {
            Stop-WithError "Kích hoạt ESP-IDF thất bại, xem $exportLog"
        }
    }
}

switch ($Command) {
    "build" {
        Write-Step "Build firmware logger"
        Invoke-Idf $LoggerFw @("build")
        $null = Test-LoggerConfig
    }
    "flash" {
        $comPort = Resolve-Port
        Write-Step "Build firmware logger"
        Invoke-Idf $LoggerFw @("build")
        if (-not (Test-LoggerConfig)) {
            Stop-WithError "Không nạp: với cấu hình này firmware dừng ngay khi khởi động. Chạy dev.bat config, sửa rồi nạp lại."
        }
        $idfArgs = @("-p", $comPort, "-b", "$FlashBaud", "flash")
        if ($NoMonitor) {
            Write-Step "Nạp firmware logger vào $comPort"
        } else {
            $idfArgs += "monitor"
            Write-Step "Nạp firmware logger vào $comPort rồi mở log (Ctrl+] để thoát)"
        }
        Invoke-Idf $LoggerFw $idfArgs
    }
    "monitor" {
        $comPort = Resolve-Port
        Write-Step "Log của board ở $comPort (Ctrl+] để thoát)"
        Invoke-Idf $LoggerFw @("-p", $comPort, "monitor")
    }
    "config" {
        Write-Step "Cấu hình logger: vào menu 'Water logger'; S để lưu, Q để thoát"
        Invoke-Idf $LoggerFw @("menuconfig")
        $null = Test-LoggerConfig
    }
    "clean" {
        foreach ($project in @($LoggerFw, $BridgeFw)) {
            if (Test-Path (Join-Path $project "build")) {
                Write-Step "Xóa build: $project"
                Invoke-Idf $project @("fullclean")
            }
        }
    }
    "bridge" {
        $comPort = Resolve-Port
        Write-Step "Build firmware cầu nối DDSU666"
        Invoke-Idf $BridgeFw @("build")
        Write-Step "Nạp firmware cầu nối vào $comPort (thay cho firmware logger)"
        Invoke-Idf $BridgeFw @("-p", $comPort, "-b", "$FlashBaud", "flash")
        Start-App $Python @("--port", $comPort, "--mode", "bridge", "--connect")
    }
    "bridge-build" {
        Write-Step "Build firmware cầu nối DDSU666"
        Invoke-Idf $BridgeFw @("build")
    }
    "app" {
        $appArgs = @()
        if ($Port) { $appArgs = @("--port", $Port.ToUpper(), "--mode", "bridge") }
        Start-App $Python $appArgs
    }
    "broker" {
        Install-BrokerPackages
        Write-Step "Chạy broker MQTT, cổng $BrokerPort (Ctrl+C để dừng)"
        $env:PORT = "$BrokerPort"
        Push-Location $Broker
        try { & node broker.mjs } finally { Pop-Location }
    }
    "test" {
        $failed = @()
        Write-Step "Test app DDSU666"
        Install-AppRequirements $Python
        Push-Location $Gui
        try { & $Python -m unittest discover -s tests } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { $failed += "app DDSU666" }

        Write-Step "Test broker MQTT (chạy tạm một broker ở cổng 18830)"
        Install-BrokerPackages
        $env:PORT = "18830"
        $brokerLog = Join-Path $env:TEMP "esp32-water-logger_broker_test.log"
        $process = Start-Process node -ArgumentList "broker.mjs" -WorkingDirectory $Broker -PassThru `
                       -WindowStyle Hidden -RedirectStandardOutput $brokerLog -RedirectStandardError "$brokerLog.err"
        Remove-Item Env:PORT
        try {
            if (-not (Wait-TcpPort 18830 10)) {
                $failed += "broker (không khởi động được, xem $brokerLog.err)"
            } else {
                Push-Location $Broker
                try { & node test.mjs "mqtt://127.0.0.1:18830" } finally { Pop-Location }
                if ($LASTEXITCODE -ne 0) { $failed += "broker" }
            }
        } finally {
            if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
        }

        if ($failed.Count) { Stop-WithError "Test thất bại: $($failed -join ', ')" }
        Write-Host ""
        Write-Host "Tất cả test đều đạt." -ForegroundColor Green
    }
}
exit 0

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
    [string]$SshHost = "",
    [int]$SshPort = 0,
    [string]$IdfPath = ""
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = $PSScriptRoot
$LoggerFw = Join-Path $Root "firmware"
$BridgeFw = Join-Path $Root "tools\ddsu666\firmware"
$Gui = Join-Path $Root "tools\ddsu666\gui"
$Broker = Join-Path $Root "tools\local-broker"
$LinuxBroker = Join-Path $Root "tools\linux-broker"

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

Broker MQTT
  broker         chạy broker để test trên máy này (tools\local-broker\, Ctrl+C để dừng)
  broker-deploy  cài broker Mosquitto lên host Linux qua SSH (cần -SshHost); cổng và tài khoản
                 MQTT của ESP32 lưu trên host (/etc/logsigt/broker.env), lần đầu script hỏi

Khác
  test           chạy toàn bộ test (app DDSU666 + broker)
  ports          liệt kê cổng COM
  help           in trang này

Tùy chọn
  -Port COM16       cổng của board; bỏ trống thì tự tìm cổng USB-serial
  -FlashBaud 115200 tốc độ nạp (mặc định 460800; giảm nếu nạp lỗi)
  -NoMonitor        flash xong không mở log
  -BrokerPort 1884  cổng broker chạy trên máy này (mặc định 1883)
  -SshHost user@IP  host Linux (Debian/Ubuntu) cho broker-deploy; tài khoản cần quyền sudo
  -SshPort 2222     cổng SSH của host (mặc định 22, hoặc Port đặt cho host đó trong ~/.ssh/config)
  -IdfPath C:\...   thư mục ESP-IDF (mặc định tìm C:\Espressif\frameworks\esp-idf-v5.*)

Ví dụ
  dev.bat config
  dev.bat flash -Port COM16
  dev.bat bridge -Port COM16
  dev.bat broker
  dev.bat broker-deploy -SshHost ubuntu@192.168.1.50
  dev.bat broker-deploy -SshHost ubuntu@192.168.1.50 -SshPort 2222
"@

$Commands = @("help", "build", "flash", "monitor", "config", "clean", "bridge", "bridge-build", "app", "broker",
              "broker-deploy", "test", "ports")

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

# ---------------------------------------------------------------- Linux broker (broker-deploy)

function Invoke-BrokerDeploy {
    if (-not $SshHost) { Stop-WithError "Thiếu -SshHost, ví dụ: dev.bat broker-deploy -SshHost ubuntu@192.168.1.50" }
    foreach ($tool in @("ssh", "tar")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            Stop-WithError "Không tìm thấy $tool. Bật OpenSSH Client trong Settings > System > Optional features."
        }
    }
    if ($SshPort -lt 0 -or $SshPort -gt 65535) { Stop-WithError "-SshPort phải là số từ 1 đến 65535." }
    $hostName = ($SshHost -split '@')[-1]
    # No -SshPort: leave the port to ssh (22, or the Port set for this host in ~/.ssh/config).
    $sshOptions = @()
    if ($SshPort) { $sshOptions = @("-p", "$SshPort") }
    $ssh = (@("ssh") + $sshOptions) -join " "

    # Send the script (LF line endings, whatever git did on checkout) as a tar stream into a fresh
    # mktemp directory, then run it with sudo in a terminal: the first run asks for the MQTT account,
    # which stays on the host in /etc/logsigt/broker.env.
    $stage = Join-Path $env:TEMP "esp32-water-logger_broker_deploy"
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory $stage | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    try {
        $script = [IO.File]::ReadAllText((Join-Path $LinuxBroker "setup-broker.sh")) -replace "`r`n", "`n"
        [IO.File]::WriteAllText((Join-Path $stage "setup-broker.sh"), $script, $utf8)
        $send = Join-Path $stage "send.cmd"
        [IO.File]::WriteAllText($send, ("@tar --format=ustar -cf - -C `"$stage`" setup-broker.sh | " +
            "$ssh $SshHost `"d=`$(mktemp -d) && tar -xf - -C `$d && echo `$d`"`r`n"), $utf8)
        Write-Step "Gửi script cài đặt lên $SshHost (ssh có thể hỏi mật khẩu đăng nhập host)"
        $remoteDir = (& cmd /c $send | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0 -or $remoteDir -notmatch '^/[\w./-]+$') {
            Stop-WithError "Không gửi được lên $SshHost. Thử '$ssh $SshHost' để kiểm tra đăng nhập và cổng SSH."
        }
    } finally {
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Step "Cài broker trên $SshHost (sudo có thể hỏi mật khẩu; lần đầu script hỏi tài khoản MQTT cho ESP32)"
    & ssh @sshOptions -t $SshHost "sudo bash $remoteDir/setup-broker.sh; s=`$?; rm -rf $remoteDir; exit `$s"
    if ($LASTEXITCODE -ne 0) { Stop-WithError "Cài broker trên $SshHost thất bại (xem log ở trên)." }

    # The port is a host setting: read it back from the broker configuration (world-readable, no secret).
    $port = & ssh @sshOptions $SshHost "sed -n 's/^listener //p' /etc/mosquitto/conf.d/logsigt.conf" |
                Select-Object -First 1
    if ($port -match '^\d+$') { $port = [int]$port } else { $port = $BrokerPort }

    Write-Step "Kiểm tra từ máy này: $hostName cổng $port"
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.ConnectAsync($hostName, $port).Wait(5000) | Out-Null
        if ($client.Connected) { Write-Host "Kết nối được tới ${hostName}:$port." -ForegroundColor Green }
        else { Write-Note "Không kết nối được tới ${hostName}:${port}: kiểm tra firewall/security group của host." }
    } catch {
        Write-Note "Không kết nối được tới ${hostName}:$port ($($_.Exception.InnerException.Message))."
    } finally { $client.Dispose() }

    $uri = Read-Sdkconfig "LOGGER_MQTT_URI"
    if ($uri -notmatch "^mqtts?://$([regex]::Escape($hostName)):$port/?$") {
        Write-Note ("Firmware đang dùng MQTT URI `"$uri`". Để logger gửi lên broker này: dev.bat config, đặt " +
                    "'MQTT broker URI' = mqtt://${hostName}:$port, rồi dev.bat flash.")
    }
    if (-not (Read-Sdkconfig "LOGGER_MQTT_USER")) {
        Write-Note ("Firmware chưa đặt 'MQTT username'/'MQTT password': đặt giống tài khoản trên host " +
                    "(xem: $ssh -t $SshHost sudo cat /etc/logsigt/broker.env), rồi dev.bat flash.")
    }
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
    "broker-deploy" {
        Invoke-BrokerDeploy
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

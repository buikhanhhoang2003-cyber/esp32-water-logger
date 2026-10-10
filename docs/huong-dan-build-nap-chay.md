# Hướng dẫn build, nạp và chạy

Mọi việc đều qua **một script ở gốc repo: `dev.bat`**. Script tự kích hoạt ESP-IDF, tự cài thư viện còn
thiếu, tự tìm cổng COM của board. `dev.bat help` (hoặc nhấp đúp `dev.bat`) in toàn bộ lệnh.

## Lệnh

| Lệnh | Việc |
|---|---|
| `dev.bat config` | Cấu hình firmware logger: Wi-Fi, MQTT, mã tòa, danh sách đồng hồ |
| `dev.bat build` | Build firmware logger |
| `dev.bat flash` | Build, nạp logger rồi mở log của board (Ctrl+] để thoát) |
| `dev.bat monitor` | Chỉ mở log của board |
| `dev.bat broker` | Chạy broker MQTT trên máy để nhận dữ liệu logger gửi lên (Ctrl+C để dừng) |
| `dev.bat bridge` | Build, nạp firmware cầu nối RS485 rồi mở app cài đặt đồng hồ DDSU666, tự kết nối |
| `dev.bat bridge-build` | Chỉ build firmware cầu nối |
| `dev.bat app` | Chỉ mở app DDSU666 (board đã có firmware cầu nối) |
| `dev.bat test` | Chạy toàn bộ test: app DDSU666 và broker |
| `dev.bat clean` | Xóa thư mục build của cả hai firmware |
| `dev.bat ports` | Liệt kê cổng COM |

| Tùy chọn | Ý nghĩa |
|---|---|
| `-Port COM16` | Cổng của board. Bỏ trống thì script tự tìm cổng USB-serial; nhiều cổng thì hỏi |
| `-FlashBaud 115200` | Tốc độ nạp, mặc định 460800; giảm khi nạp hay lỗi |
| `-NoMonitor` | `flash` xong không mở log |
| `-BrokerPort 1884` | Cổng của broker, mặc định 1883 |
| `-IdfPath C:\...` | Thư mục ESP-IDF; mặc định bản `esp-idf-v5.*` mới nhất trong `C:\Espressif\frameworks` |

**Một board chỉ chạy một firmware:** `bridge` ghi đè firmware logger, `flash` ghi đè firmware cầu nối.

## Cần cài sẵn

- ESP-IDF 5.3 (đã kiểm chứng với 5.3.3 ở `C:\Espressif\frameworks\esp-idf-v5.3.3`).
- Driver USB-serial của board (CH340 với board iMaker).
- Python 3 có Tkinter cho app DDSU666; Node.js 20 trở lên cho broker.

## 1. Chạy firmware logger

```
dev.bat config
dev.bat flash -Port COM16
```

Trong `config`, vào menu **Water logger**, đặt các mục dưới đây, rồi **S** → Enter để lưu, **Q** để thoát.

| Mục | Giá trị |
|---|---|
| Building identifier | mã tòa, ví dụ `building_1` |
| Room meter addresses (electric:water, e.g. 1:2,3:4) | **bắt buộc**: địa chỉ Modbus (số) của đồng hồ điện và nước từng phòng, ví dụ `1:2` (điện địa chỉ 1, nước địa chỉ 2); nhiều phòng: `1:2,3:4`. Không phải mã phòng hay mã tòa |
| MQTT broker URI | `mqtt://<IP máy chạy broker>:1883`; để trống = chạy offline, không gửi |
| Wi-Fi SSID / password | mạng 2.4 GHz, cùng mạng với broker |
| MQTT username / password | để trống nếu broker không yêu cầu |
| RS485 (UART, TX, RX, RTS, baud) | giữ mặc định: UART2, GPIO16, GPIO17, `-1`, 9600 — khớp board iMaker và DDSU666 |

Cấu hình lưu trong `firmware\sdkconfig` (có mật khẩu Wi-Fi nên không commit). Khi build, script cảnh báo
nếu *Room meter addresses* sai hoặc *MQTT broker URI* trống; `flash` từ chối nạp khi *Room meter addresses*
sai, vì với cấu hình đó firmware dừng ngay khi khởi động.

Log khi chạy đúng:

```
I (...) LOGGER: Starting on ESP-IDF v5.3.3-dirty
I (...) LOGGER: Building=building_1 rooms=1
I (...) MODBUS: RTU master ready, UART=2 baud=9600
I (...) MQTT: Wi-Fi connected
I (...) MQTT: Connected
W (...) LOGGER: Meter register profiles unavailable; readings are null
I (...) LOGGER: Cycle duration=... ms; 2 meters unconfigured
```

Firmware **chưa đọc đồng hồ** (hạng mục F2 chưa làm), nên mỗi chu kỳ gửi giá trị `null` với trạng thái
`"unconfigured"`. Đó là hành vi đúng của phiên bản hiện tại.

## 2. Test gửi MQTT trên máy

1. Mở một cửa sổ, chạy `dev.bat broker`. Ghi lại dòng có `(Wi-Fi)`, ví dụ `mqtt://172.16.50.146:1883`.
2. Ở cửa sổ khác: `dev.bat config`, đặt *MQTT broker URI* bằng dòng đó, Wi-Fi cùng mạng; rồi `dev.bat flash`.
3. Cửa sổ broker hiện `KẾT NỐI`, rồi cứ 10 giây một dòng `NHẬN water-logger/readings`, ví dụ:

```json
{"building_1":{"room_1":{"water":{"status":"unconfigured","total_m3":null,"flow_m3h":null},
 "electric":{"status":"unconfigured","voltage_v":null,"current_a":null,"power_w":null,"energy_kwh":null}}}}
```

Mọi bản tin được ghi vào `tools\local-broker\messages.jsonl`.

**Có số liệu điện để test mà không cần RS485:** trong `dev.bat config` → *Water logger*, bật
*Send simulated electric readings (test without meters)*. Mỗi chu kỳ, đồng hồ điện của từng phòng gửi số liệu giả
gần thật (điện áp quanh 230 V, dòng và công suất thay đổi, điện năng tăng dần) với `"status":"simulated"`:

```json
"electric":{"status":"simulated","voltage_v":229.7,"current_a":2.504,"power_w":546.5,"energy_kwh":100}
```

`dev.bat` cảnh báo mỗi lần build hoặc nạp khi tùy chọn này đang bật. **Tắt trước khi lắp thật.** Muốn broker bắt đăng nhập: đặt biến môi
trường `MQTT_USER` và `MQTT_PASS` trước khi chạy `dev.bat broker`, và điền cùng giá trị vào logger.

## 3. Cài đặt đồng hồ DDSU666

```
dev.bat bridge -Port COM16
```

Script nạp firmware cầu nối rồi mở app, kết nối luôn. Cách đấu dây, các tab của app và bảng thanh ghi: xem
[tools/ddsu666/README.md](../tools/ddsu666/README.md) và
[sơ đồ đấu nối](https://claude.ai/artifact/Qg4kkqgEiCsaYALGnU4vri). Cài đặt xong thì `dev.bat flash` để
nạp lại logger.

## 4. Xử lý lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| `Không tìm thấy ESP-IDF 5.x` | Cài ESP-IDF 5.3, hoặc chỉ đường bằng `-IdfPath` |
| Nạp báo `Failed to connect to ESP32` hoặc cổng bận | Đóng chương trình đang giữ cổng (cửa sổ log khác, app DDSU666, Arduino). Vẫn lỗi: giữ nút BOOT khi bắt đầu nạp, hoặc thêm `-FlashBaud 115200` |
| Log `Room meter addresses "..." invalid` | Đặt *Room meter addresses* dạng `điện:nước` bằng số, ví dụ `1:2`; địa chỉ 1–247, không trùng nhau |
| Log `Building identifier is empty` hoặc `MQTT telemetry topic is empty` | Điền mục tương ứng trong menu *Water logger* |
| Log `RS485 initialization failed` | Kiểm tra TX/RX/RTS trong `config`; board iMaker dùng 16/17/−1 |
| Log `Network/MQTT startup failed` | Chưa đặt *Wi-Fi SSID*, hoặc SSID/mật khẩu quá dài (32/63 ký tự) |
| Log `No broker configured; cycles will be dropped` | *MQTT broker URI* đang trống |
| Lặp `Wi-Fi connecting` | Sai SSID/mật khẩu, hoặc mạng chỉ có 5 GHz (ESP32 chỉ dùng 2.4 GHz) |
| Wi-Fi đã kết nối nhưng broker không thấy `KẾT NỐI` | IP trong URI đã đổi (xem lại dòng broker in ra), hoặc mạng chặn các thiết bị nói chuyện với nhau: cho cả máy tính và ESP32 vào hotspot điện thoại |
| `Cycle dropped (MQTT offline or outbox full)` | Bình thường ở vài chu kỳ đầu, trước khi MQTT kết nối xong |

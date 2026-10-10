# Broker MQTT cục bộ để test ESP32

Broker MQTT 3.1.1 viết bằng Node.js ([aedes](https://github.com/moscajs/aedes)), chạy ngay trên máy Windows:
không cần cài Mosquitto, không cần quyền admin. Mọi bản tin ESP32 gửi lên được in ra màn hình và ghi vào
`messages.jsonl`.

## Chạy

Từ gốc repo (lần đầu script tự chạy `npm install`):

```
dev.bat broker                     # cổng 1883
dev.bat broker -BrokerPort 1884    # cổng khác
```

Broker in ra địa chỉ để điền vào ESP32, ví dụ:

```
Broker MQTT đang chạy, cổng 1883, không yêu cầu mật khẩu.
  mqtt://172.16.50.146:1883   (Wi-Fi)        <- dùng dòng của card mạng cùng Wi-Fi với ESP32
```

Bỏ qua các dòng `VMware Network Adapter`: ESP32 không tới được các mạng ảo đó.

Muốn bắt buộc đăng nhập như broker thật (trong cùng cửa sổ cmd):

```
set MQTT_USER=dev
set MQTT_PASS=secret
dev.bat broker
```

## Kiểm tra broker (không cần ESP32)

`dev.bat test` tự bật một broker tạm và gửi thử một bản tin. Kiểm tra broker đang chạy qua IP Wi-Fi, giống
đường ESP32 sẽ đi:

```
cd tools\local-broker
node test.mjs mqtt://172.16.50.146:1883
```

Kết quả đúng: `OK: broker ... nhận và chuyển tiếp bản tin 184 byte trên "water-logger/readings" (QoS 1).`

## Trỏ firmware logger vào broker

`dev.bat config` → **Water logger**:

| Mục | Giá trị |
|---|---|
| Wi-Fi SSID / password | cùng mạng Wi-Fi với máy tính (ESP32 chỉ dùng 2.4 GHz) |
| MQTT broker URI | `mqtt://<IP Wi-Fi của máy>:1883`, ví dụ `mqtt://172.16.50.146:1883` |
| MQTT telemetry topic | `water-logger/readings` (mặc định) |
| MQTT username / password | để trống, hoặc đúng `MQTT_USER` / `MQTT_PASS` đã đặt |

Dùng `mqtt://`, không dùng `mqtts://`: broker này không bật TLS.

## Khi ESP32 không kết nối được

- **Không thấy dòng `KẾT NỐI` nào:** nhiều mạng Wi-Fi công ty, trường học hoặc quán chặn các máy trong mạng
  nói chuyện với nhau (client isolation). Cho cả máy tính và ESP32 vào hotspot của điện thoại rồi dùng IP mới
  mà broker in ra.
- **IP máy tính đổi** sau khi kết nối lại Wi-Fi (DHCP): xem lại dòng `(Wi-Fi)` broker in ra và sửa URI.
- **Tường lửa:** nếu Windows hỏi quyền cho node.exe khi chạy broker, chọn *Allow*.
- **`Cổng 1883 đang bị chương trình khác dùng`:** chạy `dev.bat broker -BrokerPort 1884` và sửa URI tương ứng.

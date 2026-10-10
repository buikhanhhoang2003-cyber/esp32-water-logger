# DDSU666 Tool — đọc & cài đặt đồng hồ CHINT DDSU666 qua RS485

Gồm hai phần:

| Thư mục | Nội dung |
|---|---|
| `firmware/` | Firmware ESP-IDF cho ESP32: cầu nối USB ⇄ RS485 (UART2, **TX = GPIO16, RX = GPIO17**) |
| `gui/` | App desktop (Python + Tkinter) đọc và ghi toàn bộ thông số có trong tài liệu DDSU666 |

```
PC (app GUI) ──USB── ESP32 UART0 ─┐
                                  └─ UART2 TX16/RX17 ── module RS485 ── A/B ── DDSU666 (chân 24 = A, 25 = B)
```

App gửi khung Modbus RTU hoàn chỉnh (đã có CRC) xuống ESP32 qua USB; ESP32 đẩy lên bus RS485 và trả lại
nguyên văn phản hồi của đồng hồ. Mọi xử lý Modbus (CRC, mã lỗi, giải mã float) nằm ở phía PC.

## Cách nhanh nhất: `dev.bat` ở gốc repo

```
dev.bat bridge -Port COM16     build + nạp firmware cầu nối + mở app, kết nối luôn
dev.bat app                    chỉ mở app (board đã có firmware cầu nối)
dev.bat bridge-build           chỉ build firmware cầu nối
dev.bat monitor -Port COM16    xem serial của ESP32 (gõ PING + Enter, Ctrl+] để thoát)
dev.bat test                   chạy test (app này và broker)
dev.bat ports                  liệt kê cổng COM
```

Bỏ `-Port` thì script tự tìm cổng USB-serial, nhiều cổng thì hỏi. `dev.bat help` liệt kê mọi lệnh và tùy
chọn (`-FlashBaud`, `-IdfPath`…). App cũng nhận tham số dòng lệnh:
`python gui\ddsu666_gui.py --port COM16 --mode bridge --connect` (`--mode`: `bridge`, `direct`, `sim`).

Các mục 2–3 dưới đây là cách làm thủ công tương đương.

## 1. Đấu dây

```
                                         Nguồn 220 V~  L ──*──┐     ┌────── L ra tải
                                                       N ─────┼──┐  │ ┌──── N ra tải
                                                              │  │  │ │
PC ══USB══ ESP32                RS485                       ┌─┴──┴──┴─┴─┐
           GPIO16 TX ───────▶ DI                            │ 1  2  3  4│  (1 = L vào *, 3 = L ra,
           GPIO17 RX ◀─────── RO        A ──────────────────┤24         │   2 = N, 4 không nối)
           (GPIOx DE ──────── DE/RE)    B ──────────────────┤25 DDSU666 │
           GND ────────────── GND                           │ 5  6 xung │
                              (trên board iMaker hoặc       └───────────┘
                               module rời)
```

| ESP32 | Bộ chuyển RS485 | DDSU666 |
|---|---|---|
| GPIO16 (TX) | DI (module tự đảo chiều: TXD) | |
| GPIO17 (RX) | RO (module tự đảo chiều: RXD) | |
| GPIOx (chỉ khi module có DE/RE) | DE + RE nối chung | |
| GND | GND | |
| | A | chân 24 (A) |
| | B | chân 25 (B) |

- Board có RS485 tích hợp (như iMaker): GPIO16/17 đã nối sẵn trong board, chỉ cần 2 dây A → 24, B → 25.
- Module có chân DE/RE: đặt `RS485 DE/RE GPIO` trong menuconfig (ví dụ 4) rồi build/nạp lại.
- MAX485 cấp 5 V đưa ra 5 V ở RO, quá mức chịu của GPIO17 (3.3 V): dùng module 3.3 V (MAX3485/SP3485) hoặc chia áp.
- Nhãn TXD/RXD của module tự đảo chiều không thống nhất giữa các hãng; không có phản hồi thì đổi chéo. Đổi chéo A/B
  cũng an toàn, chỉ mất liên lạc.
- Cấp điện đồng hồ (Hình 6): L nguồn → chân 1 (phía có *), L ra tải ← chân 3, N → chân 2. Bản DDSU666-CT (Hình 7):
  L → 1, N → 2, thứ cấp biến dòng → 9 (*) và 10. **Cắt điện trước khi đấu chân 1–4.**
- Nhiều đồng hồ: nối song song A–A, B–B thành một đường (không hình sao), 120 Ω ở đồng hồ cuối (Hình 4),
  tối đa 32 đồng hồ / 1200 m, cáp xoắn đôi có lưới ≥ 0.5 mm². Mọi thiết bị trên bus cùng baud, cùng khung, khác địa chỉ.
- Màn hình đồng hồ tự chuyển trang 5 s/lần, trong đó có `n- 8n1` (khung), `n- 011` (địa chỉ), `bAUd-3` (9600) —
  đọc đó để đặt app cho khớp.

## 2. Nạp firmware cầu nối (ESP-IDF ≥ 5.3)

Mở *ESP-IDF 5.3 PowerShell/CMD*:

```
cd tools\ddsu666\firmware          # tính từ gốc repo esp32-water-logger
idf.py set-target esp32
idf.py menuconfig          # tùy chọn: menu "RS485 bridge (DDSU666 tool)" — cổng UART, chân TX/RX/DE
idf.py -p COM5 flash
```

Kiểm tra: `idf.py -p COM5 monitor`, gõ `PING` rồi Enter, phải thấy
`@PONG RS485-BRIDGE 1.0 port=2 tx=16 rx=17 de=-1 baud=9600 fmt=8N1 bus=ok`.
Thoát monitor (Ctrl+]) trước khi mở app — mỗi cổng COM chỉ một chương trình dùng được.

Firmware này thay cho firmware logger trong lúc cài đặt đồng hồ; cài xong nạp lại firmware logger
(thư mục `firmware/` ở gốc repo).

## 3. Chạy app

```
cd tools\ddsu666\gui               # tính từ gốc repo esp32-water-logger
python -m pip install -r requirements.txt
python ddsu666_gui.py
```

hoặc `dev.bat app` ở gốc repo. Cần Python ≥ 3.8 có Tkinter (bản cài từ python.org đã có sẵn).
Chưa có phần cứng: chọn chế độ **Mô phỏng** để thử toàn bộ chức năng với một đồng hồ giả lập.

## 4. Sử dụng

**Kết nối** — chọn chế độ (ESP32 cầu nối / USB-RS485 trực tiếp / Mô phỏng), cổng COM, tốc độ, khung,
địa chỉ đồng hồ rồi bấm *Kết nối*. Mặc định đồng hồ là 9600 bps, 8N1. Đổi tốc độ/khung/địa chỉ khi đang
kết nối thì app áp dụng ngay. Không biết đồng hồ đang ở địa chỉ/tốc độ nào thì dùng tab *Quét thiết bị*.

| Tab | Chức năng |
|---|---|
| Đo lường | U, I, P (kW và W), Q, PF, F, Ep, −Ep, ComEp; đọc một lần hoặc tự động theo chu kỳ; ghi CSV |
| Cài đặt thông số | Toàn bộ Bảng 9: đọc tất cả, ghi UCode, xóa điện năng (ClrE), đổi giao thức, đổi địa chỉ, đổi baud; hiện cả các thanh ghi RESERVED |
| Thanh ghi thô | Đọc (03H) / ghi (10H) vùng bất kỳ, hiện hex, UInt16, Int16, Float32 hai thứ tự word; gửi khung hex tùy ý (tự thêm CRC) |
| Quét thiết bị | Thử lần lượt tốc độ × khung × địa chỉ (chỉ dùng lệnh đọc); nhấp đúp kết quả để dùng |
| Nhật ký | Mọi khung TX/RX có thời gian, thông báo từ ESP32; lưu ra file |

Đổi **Addr** hoặc **BAud**: app ghi, tự chuyển sang địa chỉ/tốc độ mới rồi đọc lại để xác nhận; nếu không
xác nhận được thì quay về thiết lập cũ và báo đồng hồ đang ở đâu. **ClrE** (xóa điện năng) và
**ChangeProtocol = 1** đều hỏi xác nhận vì không hoàn tác được từ app.

Thiết lập kết nối được nhớ trong `%USERPROFILE%\.ddsu666_tool.json`.

## 5. Bảng thông số (theo tài liệu)

| Địa chỉ | Mã | Ý nghĩa | Kiểu | Quyền |
|---|---|---|---|---|
| 0000H | UCode | Mật khẩu lập trình | int16 | R/W |
| 0001H | REV. | Số phiên bản | int16 | R |
| 0002H | ClrE | Ghi 1 = xóa điện năng | int16 | R/W |
| 0005H | ChangeProtocol | 2 = Modbus RTU, 1 = DL/T 645-2007 | int16 | R/W |
| 0006H | Addr | Địa chỉ truyền thông | int16 | R/W |
| 000BH | Meter type | Loại đồng hồ | int16 | R |
| 000CH | BAud | 1 = 2400, 2 = 4800, 3 = 9600 bps | int16 | R/W |
| 0003H–0004H, 0007H–000AH, 000DH–0010H | RESERVED | | int16 | |
| 2000H | U | Điện áp (V) | float32 | R |
| 2002H | I | Dòng điện (A) | float32 | R |
| 2004H | P | Công suất tác dụng (kW) | float32 | R |
| 2006H | Q | Công suất phản kháng (kvar) | float32 | R |
| 200AH | PF | Hệ số công suất | float32 | R |
| 200EH | Freq | Tần số (Hz) | float32 | R |
| 2008H, 200CH, 2010H | RESERVED | | float32 | R |
| 4000H | Ep | Điện năng thuận (kWh) | float32 | R |
| 400AH | −Ep | Điện năng ngược (kWh) | float32 | R |

Không có trong thanh ghi, chỉ đặt bằng nút trên đồng hồ (Hình 3): định dạng khung 8n2/8n1/8E1/8o1 và
giao thức 645; địa chỉ qua nút chỉ 1–99. Đồng hồ chỉ hỗ trợ lệnh 03H và 10H (không có 06H), nên app ghi mọi
thông số bằng 10H.

## 6. Những điểm tài liệu không nói rõ — cách app xử lý

- **Thứ tự word của float**: tài liệu không ghi. App mặc định ABCD (word cao trước, thông dụng nhất);
  nếu số đo vô lý, đổi *Thứ tự word float* sang CDAB.
- **1200 bps**: mục 4.3 có nhắc nhưng thanh ghi BAud chỉ định nghĩa 1–3. App chỉ cho ghi 1–3; khi quét vẫn thử 1200.
- **UCode**: không rõ có phải ghi mật khẩu trước khi đổi thông số không. Nếu lệnh ghi bị từ chối, ghi lại
  đúng mật khẩu hiện tại vào UCode rồi thử lại (ghi lại giá trị cũ không đổi mật khẩu).
- **Thanh ghi RESERVED và 4002H–4009H**: nếu đồng hồ không cho đọc liền một khối, app tự chuyển sang đọc
  từng thông số và nhớ lại cho các lần sau.
- **Địa chỉ mặc định**: ví dụ trong tài liệu hiển thị 011 — địa chỉ xuất xưởng thực tế có thể khác, dùng tab Quét.
- **Stop bit**: phụ lục A.1 viết "two stop bits" nhưng màn hình mẫu là 8n1 — app hỗ trợ cả 8N1/8N2/8E1/8O1.

## 7. Giao thức cầu nối USB (cho ai muốn tự viết phần mềm)

USB serial 115200 8N1, mỗi lệnh một dòng; phản hồi bắt đầu bằng `@`, các dòng khác là log khởi động của ESP32.

```
PING                         -> @PONG RS485-BRIDGE 1.0 port=2 tx=16 rx=17 de=-1 baud=9600 fmt=8N1 bus=ok
CFG <baud> <8N1|8N2|8E1|8O1> -> @OK baud=9600 fmt=8N1
TX <hex> [timeout_ms [len]]  -> @RX <hex> [PE] [FE] [OVF]   hoặc   @TIMEOUT
(lỗi)                        -> @ERR <lý do>
(sau khi reset)              -> @READY RS485-BRIDGE 1.0
```

`TX` gửi nguyên các byte (PC tự thêm CRC). `timeout_ms` tính từ lúc gửi xong; `len` là độ dài phản hồi mong
đợi để trả về ngay khi đủ byte. `PE`/`FE` báo lỗi parity/khung bit (thường do sai baud hoặc sai định dạng khung).

## 8. Kiểm thử

```
cd gui
python -m unittest discover -s tests
```

46 test: CRC và khung với đúng các ví dụ trong phụ lục A của tài liệu, client với đồng hồ giả lập (đọc khối,
đọc từng thanh ghi khi bị từ chối, đổi địa chỉ/baud có xác nhận, xóa điện năng, đổi giao thức, quét), và giao
thức cầu nối với một cổng COM giả mô phỏng firmware (nhiễu khởi động, ESP32 reset giữa chừng, rút cáp).

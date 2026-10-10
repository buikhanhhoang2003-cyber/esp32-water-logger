#!/usr/bin/env bash
# Installs and configures a Mosquitto MQTT broker for the water loggers on Debian/Ubuntu.
#
# From Windows: `dev.bat broker-deploy -SshHost user@host` copies this script to the host and runs
# it with sudo. The script installs itself as /usr/local/sbin/logsigt-broker-setup, so later
# changes are made on the host alone:
#
#   sudo nano /etc/logsigt/broker.env     # MQTT account of the loggers, port
#   sudo logsigt-broker-setup             # apply
#
# The broker settings live only on the host: the first run asks for the port and the loggers' MQTT
# account (without a terminal: port 1883, account esp32, generated password) and stores them in
# /etc/logsigt/broker.env (root only). The firmware must use the same username, password and port
# (dev.bat config, menu Water logger).
# Safe to run again: the broker configuration is rebuilt from that file every time.
#
# Result:
#   - Mosquitto on PORT, all interfaces, login required
#   - account DEVICE_USER may publish to water-logger/# (the loggers)
#   - account "backend" may read water-logger/#; its password is in /etc/logsigt/backend.env
#   - service logsigt-archive appends every message to /var/lib/logsigt/messages.log, so
#     records acknowledged to the loggers are kept until a real backend subscribes
set -euo pipefail

SETTINGS_FILE=/etc/logsigt/broker.env
BACKEND_ENV=/etc/logsigt/backend.env
INSTALLED=/usr/local/sbin/logsigt-broker-setup
PASSWD=/etc/mosquitto/passwd
ACL=/etc/mosquitto/acl
CONF=/etc/mosquitto/conf.d/logsigt.conf
DATA_DIR=/var/lib/logsigt
ARCHIVE=$DATA_DIR/messages.log
# Set by the Debian/Ubuntu package (log_dest file): the journal only shows service starts and stops.
BROKER_LOG=/var/log/mosquitto/mosquitto.log

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nLỖI: %s\n' "$*" >&2; exit 1; }
show_logs() {
    journalctl -u mosquitto -u logsigt-archive -n 15 --no-pager || true
    tail -n 15 "$BROKER_LOG" 2>/dev/null || true
}
random_password() { head -c 18 /dev/urandom | base64 | tr '+/' 'AB'; }
valid_port() { [[ $1 =~ ^[0-9]{1,5}$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535)); }
valid_user() { [[ $1 =~ ^[A-Za-z0-9_.-]+$ && $1 != backend ]]; }

[ "$(id -u)" -eq 0 ] || fail "cần chạy bằng root: sudo $0"
command -v apt-get >/dev/null || fail "script chỉ hỗ trợ Debian/Ubuntu (apt-get)"
command -v systemctl >/dev/null || fail "host cần systemd"

install -d -m 700 /etc/logsigt
if [ "$(readlink -f "$0")" != "$INSTALLED" ]; then
    install -m 755 "$0" "$INSTALLED"
fi

# ------------------------------------------------------------ broker port and MQTT account of the loggers

DEVICE_USER=""
DEVICE_PASS=""
PORT=1883
GENERATED=""

if [ ! -f "$SETTINGS_FILE" ]; then
    step "Cài đặt broker: cổng và tài khoản MQTT cho ESP32 (lưu ở $SETTINGS_FILE)"
    if [ -t 0 ]; then
        while :; do
            read -r -p "Cổng MQTT [1883]: " PORT
            PORT=${PORT:-1883}
            valid_port "$PORT" && break
            echo "Cổng phải là số từ 1 đến 65535."
        done
        while :; do
            read -r -p "Tên tài khoản [esp32]: " DEVICE_USER
            DEVICE_USER=${DEVICE_USER:-esp32}
            valid_user "$DEVICE_USER" && break
            echo "Tên chỉ gồm chữ, số, _ . - và không được là 'backend'."
        done
        while :; do
            read -r -s -p "Mật khẩu (Enter để tạo ngẫu nhiên): " DEVICE_PASS
            echo
            if [ -z "$DEVICE_PASS" ]; then
                DEVICE_PASS=$(random_password)
                GENERATED=1
                break
            fi
            read -r -s -p "Nhập lại mật khẩu: " again
            echo
            [ "$DEVICE_PASS" = "$again" ] && break
            echo "Hai lần nhập không khớp, nhập lại."
        done
    else
        DEVICE_USER=esp32
        DEVICE_PASS=$(random_password)
        GENERATED=1
    fi
    case $DEVICE_PASS in *$'\n'*) fail "mật khẩu không được chứa xuống dòng" ;; esac
    (
        umask 077
        cat > "$SETTINGS_FILE" <<EOF
# MQTT broker settings for the water loggers. Edit, then apply with: sudo logsigt-broker-setup
# DEVICE_USER / DEVICE_PASS must match 'MQTT username' / 'MQTT password' in the ESP32 firmware
# (dev.bat config, menu Water logger). PORT must match the port in its 'MQTT broker URI'.
PORT=$PORT
DEVICE_USER=$DEVICE_USER
DEVICE_PASS=$DEVICE_PASS
EOF
    )
fi
chmod 600 "$SETTINGS_FILE"

# KEY=value lines, read literally (never sourced): the password may contain any character.
while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case $line in
        PORT=*) PORT=${line#*=} ;;
        DEVICE_USER=*) DEVICE_USER=${line#*=} ;;
        DEVICE_PASS=*) DEVICE_PASS=${line#*=} ;;
    esac
done < "$SETTINGS_FILE"
[ -n "$DEVICE_PASS" ] || fail "$SETTINGS_FILE thiếu DEVICE_PASS"
valid_user "$DEVICE_USER" ||
    fail "DEVICE_USER trong $SETTINGS_FILE chỉ gồm chữ, số, _ . - và không được là 'backend' (đang là '$DEVICE_USER')"
valid_port "$PORT" || fail "PORT trong $SETTINGS_FILE phải là số từ 1 đến 65535 (đang là '$PORT')"
PORT=$((10#$PORT))

step "Cài Mosquitto"
export DEBIAN_FRONTEND=noninteractive
# A broken third-party source on the host (e.g. one answering 402/404) makes apt-get update fail
# although the distribution's own sources, which carry Mosquitto, were refreshed: only warn.
update_sources() {
    apt-get update -q ||
        printf '\nCẢNH BÁO: apt-get update lỗi ở một số nguồn (xem dòng E:/W: ở trên); vẫn tiếp tục cài Mosquitto.\n'
}
update_sources
# Captured first: with pipefail, `cmd | grep -q` fails when grep exits early.
has_mosquitto() { [[ $(apt-cache policy mosquitto) == *"Candidate: "[0-9]* ]]; }
if ! has_mosquitto; then
    # On Ubuntu, Mosquitto is in the "universe" component, which minimal images may leave out.
    if command -v add-apt-repository >/dev/null; then
        add-apt-repository -y universe
        update_sources
    fi
    has_mosquitto || fail "không có gói mosquitto trong nguồn apt (Ubuntu: bật universe; Debian: kiểm tra /etc/apt/sources.list)"
fi
apt-get install -y -q mosquitto mosquitto-clients
mosquitto -h 2>/dev/null | head -n 1 || true

step "Tài khoản MQTT: $DEVICE_USER (ESP32, chỉ gửi) và backend (chỉ đọc)"
BACKEND_PASS=""
if [ -f "$BACKEND_ENV" ]; then
    BACKEND_PASS=$(sed -n 's/^MQTT_PASS=//p' "$BACKEND_ENV")
fi
if [ -z "$BACKEND_PASS" ]; then
    BACKEND_PASS=$(random_password)
    (umask 077; printf 'MQTT_PASS=%s\n' "$BACKEND_PASS" > "$BACKEND_ENV")
fi
chmod 600 "$BACKEND_ENV"
# Rebuilt from the settings every run, so a renamed or removed account does not linger.
install -m 600 /dev/null "$PASSWD.new"
mosquitto_passwd -b "$PASSWD.new" "$DEVICE_USER" "$DEVICE_PASS"
mosquitto_passwd -b "$PASSWD.new" backend "$BACKEND_PASS"
mv -f "$PASSWD.new" "$PASSWD"

cat > "$ACL" <<EOF
# Managed by logsigt-broker-setup from $SETTINGS_FILE: manual changes are overwritten.

# Loggers: records, heartbeats and online/offline status (including the last will)
user $DEVICE_USER
topic write water-logger/#

# Backend and the archive service
user backend
topic read water-logger/#
EOF
chown mosquitto:mosquitto "$PASSWD" "$ACL"
chmod 600 "$PASSWD" "$ACL"

step "Cấu hình broker: cổng $PORT, bắt buộc đăng nhập"
cat > "$CONF" <<EOF
# Managed by logsigt-broker-setup from $SETTINGS_FILE: manual changes are overwritten.
listener $PORT
allow_anonymous false
password_file $PASSWD
acl_file $ACL
log_timestamp_format %Y-%m-%d %H:%M:%S

# Messages held for a persistent subscriber (the archive service) while it is down
max_queued_messages 100000
EOF

step "Service lưu dữ liệu: $ARCHIVE"
id logsigt >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin logsigt
install -d -o logsigt -g logsigt -m 750 "$DATA_DIR"
cat > /etc/systemd/system/logsigt-archive.service <<EOF
# Managed by logsigt-broker-setup.
[Unit]
Description=Append every water-logger MQTT message to $ARCHIVE
After=mosquitto.service
Wants=mosquitto.service

[Service]
User=logsigt
EnvironmentFile=$BACKEND_ENV
# Persistent session (-c, QoS 1): Mosquitto keeps messages for this client while it restarts.
ExecStart=/usr/bin/mosquitto_sub -h 127.0.0.1 -p $PORT -u backend -P \${MQTT_PASS} -i logsigt-archive -c -q 1 -F "%%I %%t %%p" -t water-logger/#
StandardOutput=append:$ARCHIVE
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/logrotate.d/logsigt-archive <<EOF
# Weekly, compressed, kept for years: the archive is the meter history.
$ARCHIVE {
    weekly
    rotate 1000
    dateext
    compress
    delaycompress
    missingok
    notifempty
    postrotate
        systemctl restart logsigt-archive >/dev/null 2>&1 || true
    endscript
}
EOF

if command -v ufw >/dev/null && [[ $(ufw status) == "Status: active"* ]]; then
    step "Mở cổng $PORT/tcp trên firewall (ufw)"
    ufw allow "$PORT/tcp"
fi

step "Khởi động"
systemctl daemon-reload
systemctl enable -q mosquitto logsigt-archive
systemctl restart mosquitto
sleep 1
if ! systemctl is-active -q mosquitto; then
    show_logs
    fail "Mosquitto không chạy (xem log ở trên)"
fi
systemctl restart logsigt-archive

step "Tự kiểm tra: gửi bằng tài khoản $DEVICE_USER, đợi bản tin xuất hiện trong $ARCHIVE"
marker="selftest-$(date +%s)-$$"
found=0
for attempt in 1 2 3 4 5; do
    sleep 2
    mosquitto_pub -h 127.0.0.1 -p "$PORT" -u "$DEVICE_USER" -P "$DEVICE_PASS" -q 1 \
        -t water-logger/selftest -m "{\"selftest\":\"$marker\",\"attempt\":$attempt}" || true
    sleep 1
    if grep -q "$marker" "$ARCHIVE" 2>/dev/null; then
        found=1
        break
    fi
done
if [ "$found" -ne 1 ]; then
    show_logs
    fail "bản tin thử không tới $ARCHIVE (xem log ở trên)"
fi
echo "OK: đăng nhập, phân quyền topic và lưu dữ liệu đều hoạt động (topic thử: water-logger/selftest)."

printf '\nXONG. Broker MQTT chạy ở cổng %s.\n' "$PORT"
for ip in $(hostname -I 2>/dev/null); do
    case $ip in *.*) printf '  URI cho ESP32:     mqtt://%s:%s\n' "$ip" "$PORT" ;; esac
done
printf '  Tài khoản ESP32:   %s (mật khẩu: sudo cat %s)\n' "$DEVICE_USER" "$SETTINGS_FILE"
if [ -n "$GENERATED" ]; then
    printf '  Mật khẩu vừa tạo:  %s\n' "$DEVICE_PASS"
fi
printf '  Tài khoản đọc:     backend (mật khẩu: sudo cat %s)\n' "$BACKEND_ENV"
printf '  Dữ liệu nhận được: %s (tail -f để xem trực tiếp)\n' "$ARCHIVE"
printf '  Log broker:        sudo tail -f %s\n' "$BROKER_LOG"
printf '  Đổi tài khoản/cổng: sudo nano %s, rồi sudo %s\n' "$SETTINGS_FILE" "$(basename "$INSTALLED")"
printf '\nTrên Windows: dev.bat config (menu Water logger) đặt MQTT username/password như trên và URI mqtt://<IP>:%s, rồi dev.bat flash.\n' "$PORT"

# Firmware execution flow

```mermaid
flowchart TD
    Boot["ESP32 boot: app_main()"] --> Validate{"Building ID, MQTT topic, and room IDs valid?"}
    Validate -->|No| ConfigError["Log invalid configuration and return"]
    Validate -->|Yes| ModbusInit["modbus_init(): validate GPIOs, create master, set UART pins/mode, start master"]
    ModbusInit --> ModbusOK{"Modbus initialization succeeded?"}
    ModbusOK -->|No| Fatal["Log RS485 initialization error and return"]
    ModbusOK -->|Yes| Network["start_network(): initialize NVS, netif, event loop, then mqtt_init()"]
    Network --> NetworkOK{"Network/MQTT initialization succeeded?"}
    NetworkOK -->|No| Offline["Log startup failure; continue cycles offline"]
    NetworkOK -->|Yes| InitReadings["Set each room reading status to unconfigured"]
    Offline --> InitReadings
    InitReadings --> MissingProfiles["Log that meter register profiles are unavailable"]
    MissingProfiles --> Cycle["run_cycle(): start cycle timer"]
    Cycle --> Serialize["telemetry_serialize(): serialize each room's current reading"]
    Serialize --> JsonOK{"Payload created within 4096-byte limit?"}
    JsonOK -->|No| JsonError["Log serialization failure; skip publish"]
    JsonOK -->|Yes| Send["mqtt_send(): enqueue JSON if MQTT is connected"]
    Send --> SendOK{"Enqueue succeeded?"}
    SendOK -->|No| Drop["Log dropped cycle; free payload"]
    SendOK -->|Yes| Free["Free payload"]
    JsonError --> Delay
    Drop --> Delay
    Free --> Delay["Wait for remaining LOGGER_POLL_MS, if any"]
    Delay --> Cycle

    Note["No sequential meter polling occurs: main.c does not call modbus_read()."]
    Note -.-> Serialize
```

NVS is erased and initialized again only when `nvs_flash_init()` reports no free
pages or a new NVS version. An empty broker URI is an explicit offline mode and
does not select a substitute broker. ESP-MQTT and the Wi-Fi event handler manage
reconnection after their clients have started.

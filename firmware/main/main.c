#include <math.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "esp_app_desc.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#include "ddsu666.h"
#include "meter.h"
#include "modbus.h"
#include "mqtt.h"
#include "record_queue.h"
#include "telemetry.h"

#define PAYLOAD_LIMIT 4096
#define ACK_TIMEOUT_MS 5000
/* Earlier than November 2023 means SNTP has not set the clock yet. */
#define CLOCK_VALID_AFTER 1700000000

static const char *TAG = "LOGGER";
static char device_id[33];
/* Static: a record buffer this size does not fit on the main task stack. */
static char record[PAYLOAD_LIMIT + 1];
/* Sequence numbers while the queue partition is unavailable (restart from 0 every boot). */
static uint32_t unqueued_seq;

static void on_clock_set(struct timeval *tv)
{
    char ts[TELEMETRY_TS_LEN];

    telemetry_format_time(tv->tv_sec, ts);
    ESP_LOGI(TAG, "Clock set by SNTP: %s", ts);
}

static void start_sntp(void)
{
    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_LOGGER_SNTP_SERVER);
    esp_err_t err;

    if (CONFIG_LOGGER_SNTP_SERVER[0] == '\0') {
        ESP_LOGW(TAG, "No SNTP server: records carry \"ts\": null");
        return;
    }
    config.sync_cb = on_clock_set;
    err = esp_netif_sntp_init(&config);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SNTP start failed: %s", esp_err_to_name(err));
    }
}

static esp_err_t start_network(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        err = nvs_flash_erase();
        if (err == ESP_OK) {
            err = nvs_flash_init();
        }
    }
    if (err != ESP_OK) {
        return err;
    }
    err = esp_netif_init();
    if (err != ESP_OK) {
        return err;
    }
    err = esp_event_loop_create_default();
    if (err != ESP_OK) {
        return err;
    }
    err = mqtt_init(device_id);
    if (err == ESP_OK && CONFIG_LOGGER_MQTT_URI[0] != '\0') {
        start_sntp();
    }
    return err;
}

static void make_device_id(void)
{
    uint8_t mac[6] = {0};

    if (CONFIG_LOGGER_DEVICE_ID[0] != '\0') {
        snprintf(device_id, sizeof(device_id), "%s", CONFIG_LOGGER_DEVICE_ID);
        return;
    }
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(device_id, sizeof(device_id), "ESP32_%02X%02X%02X", mac[3], mac[4], mac[5]);
}

static bool clock_now(int64_t *epoch_s)
{
    time_t now = time(NULL);

    if (now < CLOCK_VALID_AFTER) {
        return false;
    }
    *epoch_s = (int64_t)now;
    return true;
}

static int64_t uptime_s(void)
{
    return esp_timer_get_time() / 1000000;
}

#if CONFIG_LOGGER_FAKE_ELECTRIC
static measurement_t rounded(double value, double scale)
{
    return (measurement_t){.valid = true, .value = round(value * scale) / scale};
}

/* Plausible single-phase values so Wi-Fi and MQTT can be tested without meters. */
static void simulate_electric(room_reading_t readings[], size_t count)
{
    static double energy_kwh[METER_MAX_ROOMS];
    static int64_t last_us;
    int64_t now_us = esp_timer_get_time();
    double hours = last_us != 0 ? (double)(now_us - last_us) / 3.6e9 : 0.0;
    double t = (double)now_us / 1e6;

    last_us = now_us;
    for (size_t index = 0; index < count; ++index) {
        double phase = (double)index;
        double voltage = 229.0 + 3.0 * sin(t / 50.0 + phase);
        double current = 2.0 + 1.5 * sin(t / 35.0 + 2.0 * phase);
        double power = voltage * current * 0.95;
        electric_reading_t *electric = &readings[index].electric;

        if (energy_kwh[index] == 0.0) {
            energy_kwh[index] = 100.0 + 25.0 * phase;
        }
        energy_kwh[index] += power / 1000.0 * hours;
        electric->status = "simulated";
        electric->voltage_v = rounded(voltage, 10);
        electric->current_a = rounded(current, 1000);
        electric->power_w = rounded(power, 10);
        electric->energy_kwh = rounded(energy_kwh[index], 100);
    }
}
#endif

static void read_meters(const room_address_t rooms[], room_reading_t readings[], size_t count)
{
#if CONFIG_LOGGER_FAKE_ELECTRIC
    (void)rooms;
    simulate_electric(readings, count);
#else
    /* Electric meters are CHINT DDSU666; there is no water meter profile yet, so water
     * readings stay "unconfigured". A failing meter only marks its own reading. */
    for (size_t index = 0; index < count; ++index) {
        ddsu666_read(rooms[index].electric_slave, &readings[index].electric);
    }
#endif
}

static void tally(const char *status, unsigned *ok, unsigned *failed, unsigned *unconfigured)
{
    if (strcmp(status, "ok") == 0 || strcmp(status, "simulated") == 0) {
        ++*ok;
    } else if (strcmp(status, "unconfigured") == 0) {
        ++*unconfigured;
    } else {
        ++*failed;
    }
}

/* Turns this cycle's readings into a record and queues it; without a queue partition the
 * record is sent right away and lost if the broker does not acknowledge it. */
static void store_record(const room_reading_t readings[], size_t count)
{
    char ts[TELEMETRY_TS_LEN];
    int64_t now;
    esp_err_t err;
    telemetry_meta_t meta = {
        .device_id = device_id,
        .building = CONFIG_LOGGER_BUILDING_ID,
        .seq = record_queue_ready() ? record_queue_next_seq() : unqueued_seq++,
        .boot = record_queue_boot(),
        .uptime_s = uptime_s(),
    };

    if (clock_now(&now)) {
        telemetry_format_time(now, ts);
        meta.ts = ts;
    }
    char *payload = telemetry_serialize(&meta, readings, count, PAYLOAD_LIMIT);
    if (payload == NULL) {
        ESP_LOGE(TAG, "Telemetry serialization failed or payload too large");
        return;
    }
    if (record_queue_ready()) {
        err = record_queue_push(payload);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Record %lu not stored: %s", (unsigned long)meta.seq, esp_err_to_name(err));
        }
    } else {
        err = mqtt_publish_acked(CONFIG_LOGGER_MQTT_TOPIC, payload, CONFIG_LOGGER_MQTT_QOS, ACK_TIMEOUT_MS);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Record %lu dropped (no queue, not acknowledged): %s",
                     (unsigned long)meta.seq, esp_err_to_name(err));
        }
    }
    telemetry_free(payload);
}

/* Sends queued records oldest first; each leaves the queue only once the broker acknowledged
 * it. Stops when the queue is empty, MQTT is down, a record is not acknowledged or the
 * deadline has passed. */
static unsigned drain_queue(int64_t deadline_us)
{
    unsigned sent = 0;
    int64_t now = 0;
    bool clock = clock_now(&now);
    int64_t uptime = uptime_s();

    while (record_queue_ready() && mqtt_is_connected() && esp_timer_get_time() < deadline_us) {
        uint32_t seq;
        esp_err_t err = record_queue_peek(record, sizeof(record), &seq);

        if (err != ESP_OK) {
            if (err != ESP_ERR_NOT_FOUND) {
                ESP_LOGE(TAG, "Queue read failed: %s", esp_err_to_name(err));
            }
            break;
        }
        /* Recorded before SNTP set the clock: derive its time from the uptime it carries. */
        char *timed = clock ? telemetry_fill_ts(record, record_queue_boot(), uptime, now, PAYLOAD_LIMIT) : NULL;
        err = mqtt_publish_acked(CONFIG_LOGGER_MQTT_TOPIC, timed != NULL ? timed : record,
                                 CONFIG_LOGGER_MQTT_QOS, ACK_TIMEOUT_MS);
        telemetry_free(timed);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Record %lu not acknowledged, kept for retry: %s", (unsigned long)seq, esp_err_to_name(err));
            break;
        }
        err = record_queue_pop();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Record %lu sent but not removed from the queue: %s",
                     (unsigned long)seq, esp_err_to_name(err));
            break;
        }
        ++sent;
    }
    return sent;
}

static const char *reset_reason(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON:
        return "power_on";
    case ESP_RST_EXT:
        return "external";
    case ESP_RST_SW:
        return "software";
    case ESP_RST_PANIC:
        return "panic";
    case ESP_RST_INT_WDT:
        return "interrupt_watchdog";
    case ESP_RST_TASK_WDT:
        return "task_watchdog";
    case ESP_RST_WDT:
        return "watchdog";
    case ESP_RST_DEEPSLEEP:
        return "deep_sleep";
    case ESP_RST_BROWNOUT:
        return "brownout";
    default:
        return "other";
    }
}

static bool send_heartbeat(const room_address_t rooms[], const room_reading_t readings[], size_t count)
{
    char ts[TELEMETRY_TS_LEN];
    int64_t now;
    esp_err_t err;
    telemetry_heartbeat_t heartbeat = {
        .device_id = device_id,
        .building = CONFIG_LOGGER_BUILDING_ID,
        .firmware = esp_app_get_description()->version,
        .reset_reason = reset_reason(),
        .boot = record_queue_boot(),
        .uptime_s = uptime_s(),
        .free_heap = esp_get_free_heap_size(),
        .min_free_heap = esp_get_minimum_free_heap_size(),
        .queue_pending = record_queue_pending(),
        .queue_dropped = record_queue_dropped(),
    };

    if (clock_now(&now)) {
        telemetry_format_time(now, ts);
        heartbeat.ts = ts;
    }
    heartbeat.rssi_valid = mqtt_wifi_rssi(&heartbeat.rssi);
    char *payload = telemetry_heartbeat(&heartbeat, rooms, readings, count, PAYLOAD_LIMIT);
    if (payload == NULL) {
        ESP_LOGE(TAG, "Heartbeat serialization failed");
        return false;
    }
    err = mqtt_publish(CONFIG_LOGGER_MQTT_HEARTBEAT_TOPIC, payload, 0, false);
    telemetry_free(payload);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Heartbeat not sent: %s", esp_err_to_name(err));
        return false;
    }
    return true;
}

static void run_cycle(const room_address_t rooms[], room_reading_t readings[], size_t count)
{
    static bool heartbeat_sent;
    static int64_t heartbeat_us;
    int64_t start_us = esp_timer_get_time();
    /* Leave a fifth of the interval so a long backlog does not delay the next reading. */
    int64_t deadline_us = start_us + (int64_t)CONFIG_LOGGER_POLL_MS * 800;
    unsigned ok = 0;
    unsigned failed = 0;
    unsigned unconfigured = 0;
    unsigned sent;

    read_meters(rooms, readings, count);
    for (size_t index = 0; index < count; ++index) {
        tally(readings[index].electric.status, &ok, &failed, &unconfigured);
        tally(readings[index].water.status, &ok, &failed, &unconfigured);
    }
    store_record(readings, count);
    if (CONFIG_LOGGER_MQTT_HEARTBEAT_TOPIC[0] != '\0' && mqtt_is_connected() &&
        (!heartbeat_sent || start_us - heartbeat_us >= (int64_t)CONFIG_LOGGER_HEARTBEAT_S * 1000000)) {
        if (send_heartbeat(rooms, readings, count)) {
            heartbeat_sent = true;
            heartbeat_us = start_us;
        }
    }
    sent = drain_queue(deadline_us);
    int64_t elapsed_ms = (esp_timer_get_time() - start_us) / 1000;
    ESP_LOGI(TAG, "Cycle duration=%lld ms; meters ok=%u failed=%u unconfigured=%u; records sent=%u pending=%lu",
             (long long)elapsed_ms, ok, failed, unconfigured, sent, (unsigned long)record_queue_pending());
    if (elapsed_ms < CONFIG_LOGGER_POLL_MS) {
        vTaskDelay(pdMS_TO_TICKS(CONFIG_LOGGER_POLL_MS - elapsed_ms) + 1);
    }
}

void app_main(void)
{
    /* Static: 16 readings take about 1.8 KB of stack otherwise. */
    static room_address_t rooms[METER_MAX_ROOMS];
    static room_reading_t readings[METER_MAX_ROOMS];
    size_t count = 0;
    esp_err_t err;

    make_device_id();
    ESP_LOGI(TAG, "Starting %s firmware %s on ESP-IDF %s", device_id, esp_app_get_description()->version,
             esp_get_idf_version());
    if (CONFIG_LOGGER_BUILDING_ID[0] == '\0') {
        ESP_LOGE(TAG, "Building identifier is empty; set it in the Water logger menu");
        return;
    }
    if (CONFIG_LOGGER_MQTT_TOPIC[0] == '\0') {
        ESP_LOGE(TAG, "MQTT telemetry topic is empty; set it in the Water logger menu");
        return;
    }
    if (!meter_parse_rooms(CONFIG_LOGGER_ROOMS, rooms, METER_MAX_ROOMS, &count)) {
        ESP_LOGE(TAG, "Room meter addresses \"%s\" invalid: expected electric:water Modbus addresses 1-247, "
                      "all different, e.g. 1:2 or 1:2,3:4 (max %d rooms)", CONFIG_LOGGER_ROOMS, METER_MAX_ROOMS);
        return;
    }
    ESP_LOGI(TAG, "Building=%s rooms=%u", CONFIG_LOGGER_BUILDING_ID, (unsigned)count);
    err = record_queue_init(CONFIG_LOGGER_QUEUE_CAPACITY);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Record queue: boot=%lu pending=%lu next seq=%lu capacity=%d",
                 (unsigned long)record_queue_boot(), (unsigned long)record_queue_pending(),
                 (unsigned long)record_queue_next_seq(), CONFIG_LOGGER_QUEUE_CAPACITY);
    } else {
        ESP_LOGE(TAG, "Record queue unavailable (%s): records are sent directly and lost while offline; "
                      "flash the partition table (dev.bat flash)", esp_err_to_name(err));
    }
    err = modbus_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "RS485 initialization failed: %s", esp_err_to_name(err));
        return;
    }
    err = start_network();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Network/MQTT startup failed; records stay in the queue: %s", esp_err_to_name(err));
    }
    for (size_t index = 0; index < count; ++index) {
        readings[index].electric.status = "unconfigured";
        readings[index].water.status = "unconfigured";
    }
#if CONFIG_LOGGER_FAKE_ELECTRIC
    ESP_LOGW(TAG, "Electric readings are SIMULATED (test option); water readings are null");
#else
    ESP_LOGI(TAG, "Electric meters read as CHINT DDSU666; no water meter profile yet (water readings null)");
#endif
    for (unsigned cycle = 0;; ++cycle) {
        run_cycle(rooms, readings, count);
        if (cycle == 0) {
            ESP_LOGI(TAG, "Main task stack headroom after first cycle: %u bytes",
                     (unsigned)uxTaskGetStackHighWaterMark(NULL));
        }
    }
}

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#include "meter.h"
#include "modbus.h"
#include "mqtt.h"
#include "telemetry.h"

#define PAYLOAD_LIMIT 4096

static const char *TAG = "LOGGER";

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
    return mqtt_init();
}

static void run_cycle(const room_reading_t readings[], size_t count)
{
    int64_t start_us = esp_timer_get_time();
    char *payload = telemetry_serialize(CONFIG_LOGGER_BUILDING_ID, readings, count, PAYLOAD_LIMIT);

    if (payload == NULL) {
        ESP_LOGE(TAG, "Telemetry serialization failed or payload too large");
    } else {
        esp_err_t err = mqtt_send(payload);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Cycle dropped (MQTT offline or outbox full): %s", esp_err_to_name(err));
        }
        telemetry_free(payload);
    }
    int64_t elapsed_ms = (esp_timer_get_time() - start_us) / 1000;
    ESP_LOGI(TAG, "Cycle duration=%lld ms; %u meters unconfigured",
             (long long)elapsed_ms, (unsigned)(count * 2));
    if (elapsed_ms < CONFIG_LOGGER_POLL_MS) {
        vTaskDelay(pdMS_TO_TICKS(CONFIG_LOGGER_POLL_MS - elapsed_ms) + 1);
    }
}

void app_main(void)
{
    room_address_t rooms[METER_MAX_ROOMS] = {0};
    room_reading_t readings[METER_MAX_ROOMS] = {0};
    size_t count = 0;
    esp_err_t err;

    ESP_LOGI(TAG, "Starting on ESP-IDF %s", esp_get_idf_version());
    if (CONFIG_LOGGER_BUILDING_ID[0] == '\0' || CONFIG_LOGGER_MQTT_TOPIC[0] == '\0' ||
        !meter_parse_rooms(CONFIG_LOGGER_ROOMS, rooms, METER_MAX_ROOMS, &count)) {
        ESP_LOGE(TAG, "Invalid building, topic or room IDs; check Water logger menu");
        return;
    }
    ESP_LOGI(TAG, "Building=%s rooms=%u", CONFIG_LOGGER_BUILDING_ID, (unsigned)count);
    err = modbus_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "RS485 initialization failed: %s", esp_err_to_name(err));
        return;
    }
    err = start_network();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Network/MQTT startup failed; cycles continue offline: %s", esp_err_to_name(err));
    }
    for (size_t index = 0; index < count; ++index) {
        readings[index].electric.status = "unconfigured";
        readings[index].water.status = "unconfigured";
    }
    ESP_LOGW(TAG, "Meter register profiles unavailable; readings are null");
    while (true) {
        run_cycle(readings, count);
    }
}

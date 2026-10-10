#include "mqtt.h"

#include <stdbool.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_wifi_default.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "mqtt_client.h"
#include "sdkconfig.h"

#define KEEPALIVE_S 30
#define ACK_QUEUE_LENGTH 8

static const char *TAG = "MQTT";
static esp_mqtt_client_handle_t client;
static esp_netif_t *sta_netif;
static esp_event_handler_instance_t wifi_handler;
static esp_event_handler_instance_t ip_handler;
static bool wifi_initialized;
static bool wifi_started;
static bool mqtt_started;
static atomic_bool connected;
static atomic_bool stopping;
static QueueHandle_t acks;          /* msg_id of every acknowledged publish */
static char client_id[33];
static char status_topic[128];
static char online_message[96];
static char offline_message[96];

static const char *disconnect_hint(uint8_t reason)
{
    switch (reason) {
    case WIFI_REASON_NO_AP_FOUND:
        return "SSID not found: check the name and range, and that the network has 2.4 GHz (ESP32 has no 5 GHz)";
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
        return "authentication failed: check the Wi-Fi password";
    case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY:
    case WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD:
        return "network security not supported: use WPA2/WPA3-Personal, not Enterprise";
    case WIFI_REASON_NO_AP_FOUND_IN_RSSI_THRESHOLD:
    case WIFI_REASON_BEACON_TIMEOUT:
        return "signal too weak or lost";
    default:
        return "see wifi_err_reason_t in esp_wifi_types_generic.h";
    }
}

static void on_wifi(void *arg, esp_event_base_t base, int32_t event_id, void *data)
{
    (void)arg;
    if (atomic_load(&stopping)) {
        return;
    }
    if (base == WIFI_EVENT && (event_id == WIFI_EVENT_STA_START ||
                               event_id == WIFI_EVENT_STA_DISCONNECTED)) {
        if (event_id == WIFI_EVENT_STA_DISCONNECTED) {
            const wifi_event_sta_disconnected_t *event = data;
            ESP_LOGW(TAG, "Wi-Fi not connected, reason %u: %s", event->reason, disconnect_hint(event->reason));
        }
        ESP_LOGW(TAG, "Wi-Fi connecting to \"%s\"", CONFIG_LOGGER_WIFI_SSID);
        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Wi-Fi connect request failed: %s", esp_err_to_name(err));
        }
    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI(TAG, "Wi-Fi connected");
    }
}

static esp_err_t start_wifi(void)
{
    esp_err_t err;
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    wifi_config_t wifi = {0};

    if (CONFIG_LOGGER_WIFI_SSID[0] == '\0' || strlen(CONFIG_LOGGER_WIFI_SSID) > 32 ||
        strlen(CONFIG_LOGGER_WIFI_PASSWORD) > 63) {
        return ESP_ERR_INVALID_ARG;
    }
    sta_netif = esp_netif_create_default_wifi_sta();
    if (sta_netif == NULL) {
        return ESP_ERR_NO_MEM;
    }
    err = esp_wifi_init(&init);
    if (err != ESP_OK) {
        return err;
    }
    wifi_initialized = true;
    err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               on_wifi, NULL, &wifi_handler);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               on_wifi, NULL, &ip_handler);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) {
        return err;
    }
    memcpy(wifi.sta.ssid, CONFIG_LOGGER_WIFI_SSID, strlen(CONFIG_LOGGER_WIFI_SSID));
    memcpy(wifi.sta.password, CONFIG_LOGGER_WIFI_PASSWORD, strlen(CONFIG_LOGGER_WIFI_PASSWORD));
    err = esp_wifi_set_config(WIFI_IF_STA, &wifi);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_start();
    wifi_started = (err == ESP_OK);
    return err;
}

static void on_mqtt(void *arg, esp_event_base_t base, int32_t event_id, void *data)
{
    esp_mqtt_event_handle_t event = data;

    (void)arg;
    (void)base;
    if (event_id == MQTT_EVENT_CONNECTED) {
        atomic_store(&connected, true);
        ESP_LOGI(TAG, "Connected as %s", client_id);
        if (status_topic[0] != '\0' &&
            esp_mqtt_client_enqueue(event->client, status_topic, online_message, 0, 1, 1, true) < 0) {
            ESP_LOGW(TAG, "Online status not queued");
        }
    } else if (event_id == MQTT_EVENT_DISCONNECTED || event_id == MQTT_EVENT_ERROR) {
        atomic_store(&connected, false);
        ESP_LOGW(TAG, "Disconnected, event=%ld", (long)event_id);
    } else if (event_id == MQTT_EVENT_PUBLISHED && acks != NULL) {
        xQueueSend(acks, &event->msg_id, 0);
    }
}

esp_err_t mqtt_init(const char *device_id)
{
    esp_err_t err;
    esp_mqtt_client_config_t config = {0};

    if (device_id == NULL || device_id[0] == '\0' || strlen(device_id) >= sizeof(client_id)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (client != NULL || sta_netif != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    atomic_store(&stopping, false);
    if (CONFIG_LOGGER_MQTT_URI[0] == '\0') {
        ESP_LOGW(TAG, "No broker configured; records stay in the queue");
        return ESP_OK;
    }
    snprintf(client_id, sizeof(client_id), "%s", device_id);
    if (CONFIG_LOGGER_MQTT_STATUS_TOPIC[0] != '\0') {
        snprintf(status_topic, sizeof(status_topic), "%s/%s", CONFIG_LOGGER_MQTT_STATUS_TOPIC, device_id);
        snprintf(online_message, sizeof(online_message), "{\"device_id\":\"%s\",\"online\":true}", device_id);
        snprintf(offline_message, sizeof(offline_message), "{\"device_id\":\"%s\",\"online\":false}", device_id);
        config.session.last_will.topic = status_topic;
        config.session.last_will.msg = offline_message;
        config.session.last_will.qos = 1;
        config.session.last_will.retain = 1;
    }
    acks = xQueueCreate(ACK_QUEUE_LENGTH, sizeof(int));
    if (acks == NULL) {
        err = ESP_ERR_NO_MEM;
        goto fail;
    }
    err = start_wifi();
    if (err != ESP_OK) {
        goto fail;
    }
    config.broker.address.uri = CONFIG_LOGGER_MQTT_URI;
    config.broker.verification.crt_bundle_attach = esp_crt_bundle_attach;
    config.credentials.client_id = client_id;
    config.credentials.username = CONFIG_LOGGER_MQTT_USER;
    config.credentials.authentication.password = CONFIG_LOGGER_MQTT_PASSWORD;
    config.session.keepalive = KEEPALIVE_S;
    config.network.reconnect_timeout_ms = CONFIG_LOGGER_MQTT_RECONNECT_MS;
    /* Unacknowledged records live in the flash queue; the outbox only holds messages in flight. */
    config.outbox.limit = 8192;
    client = esp_mqtt_client_init(&config);
    if (client == NULL) {
        ESP_LOGE(TAG,
                 "MQTT client initialization failed; check broker URI, configuration, and available memory");
        err = ESP_FAIL;
        goto fail;
    }
    err = esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, on_mqtt, NULL);
    if (err != ESP_OK) {
        goto fail;
    }
    err = esp_mqtt_client_start(client);
    if (err != ESP_OK) {
        goto fail;
    }
    mqtt_started = true;
    return ESP_OK;

fail:
    mqtt_deinit();
    return err;
}

bool mqtt_is_connected(void)
{
    return client != NULL && atomic_load(&connected);
}

static esp_err_t publish_error(int result)
{
    return result == -2 ? ESP_ERR_NO_MEM : ESP_FAIL;
}

esp_err_t mqtt_publish_acked(const char *topic, const char *payload, int qos, uint32_t timeout_ms)
{
    TickType_t deadline;
    int msg_id;
    int acked;

    if (topic == NULL || topic[0] == '\0' || payload == NULL || qos < 0 || qos > 2) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!mqtt_is_connected()) {
        return ESP_ERR_INVALID_STATE;
    }
    /* Acknowledgements of earlier publishes that timed out are of no use any more. */
    xQueueReset(acks);
    msg_id = esp_mqtt_client_publish(client, topic, payload, (int)strlen(payload), qos, 0);
    if (msg_id < 0) {
        return publish_error(msg_id);
    }
    if (qos == 0) {
        return ESP_OK;
    }
    deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
    for (;;) {
        TickType_t remaining = deadline - xTaskGetTickCount();
        if ((int32_t)remaining <= 0 || xQueueReceive(acks, &acked, remaining) != pdTRUE) {
            return ESP_ERR_TIMEOUT;
        }
        if (acked == msg_id) {
            return ESP_OK;
        }
    }
}

esp_err_t mqtt_publish(const char *topic, const char *payload, int qos, bool retain)
{
    int msg_id;

    if (topic == NULL || topic[0] == '\0' || payload == NULL || qos < 0 || qos > 2) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!mqtt_is_connected()) {
        return ESP_ERR_INVALID_STATE;
    }
    /* store=true: without it esp-mqtt refuses to enqueue QoS 0 messages (returns -1). */
    msg_id = esp_mqtt_client_enqueue(client, topic, payload, (int)strlen(payload), qos, retain ? 1 : 0, true);
    return msg_id < 0 ? publish_error(msg_id) : ESP_OK;
}

bool mqtt_wifi_rssi(int *rssi)
{
    wifi_ap_record_t ap;

    if (rssi == NULL || !wifi_started || esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
        return false;
    }
    *rssi = ap.rssi;
    return true;
}

void mqtt_deinit(void)
{
    atomic_store(&stopping, true);
    atomic_store(&connected, false);
    if (client != NULL) {
        if (mqtt_started) {
            esp_err_t err = esp_mqtt_client_stop(client);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "MQTT stop failed: %s", esp_err_to_name(err));
            }
            mqtt_started = false;
        }
        esp_mqtt_client_destroy(client);
        client = NULL;
    }
    if (wifi_started) {
        esp_wifi_stop();
        wifi_started = false;
    }
    if (ip_handler != NULL) {
        esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, ip_handler);
        ip_handler = NULL;
    }
    if (wifi_handler != NULL) {
        esp_event_handler_instance_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_handler);
        wifi_handler = NULL;
    }
    if (wifi_initialized) {
        esp_wifi_deinit();
        wifi_initialized = false;
    }
    if (sta_netif != NULL) {
        esp_netif_destroy_default_wifi(sta_netif);
        sta_netif = NULL;
    }
    if (acks != NULL) {
        vQueueDelete(acks);
        acks = NULL;
    }
}

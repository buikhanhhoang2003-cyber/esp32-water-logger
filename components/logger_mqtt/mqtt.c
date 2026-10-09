#include "mqtt.h"

#include <stdbool.h>
#include <stdatomic.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_wifi_default.h"
#include "mqtt_client.h"
#include "sdkconfig.h"

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

static void on_wifi(void *arg, esp_event_base_t base, int32_t event_id, void *data)
{
    (void)arg;
    (void)data;
    if (atomic_load(&stopping)) {
        return;
    }
    if (base == WIFI_EVENT && (event_id == WIFI_EVENT_STA_START ||
                               event_id == WIFI_EVENT_STA_DISCONNECTED)) {
        ESP_LOGW(TAG, "Wi-Fi connecting");
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
    (void)arg;
    (void)base;
    (void)data;
    if (event_id == MQTT_EVENT_CONNECTED) {
        atomic_store(&connected, true);
        ESP_LOGI(TAG, "Connected");
    } else if (event_id == MQTT_EVENT_DISCONNECTED || event_id == MQTT_EVENT_ERROR) {
        atomic_store(&connected, false);
        ESP_LOGW(TAG, "Disconnected, event=%ld", (long)event_id);
    }
}

esp_err_t mqtt_init(void)
{
    esp_err_t err;
    esp_mqtt_client_config_t config = {0};

    if (client != NULL || sta_netif != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    atomic_store(&stopping, false);
    if (CONFIG_LOGGER_MQTT_URI[0] == '\0') {
        ESP_LOGW(TAG, "No broker configured; cycles will be dropped");
        return ESP_OK;
    }
    err = start_wifi();
    if (err != ESP_OK) {
        goto fail;
    }
    config.broker.address.uri = CONFIG_LOGGER_MQTT_URI;
    config.broker.verification.crt_bundle_attach = esp_crt_bundle_attach;
    config.credentials.username = CONFIG_LOGGER_MQTT_USER;
    config.credentials.authentication.password = CONFIG_LOGGER_MQTT_PASSWORD;
    config.network.reconnect_timeout_ms = CONFIG_LOGGER_MQTT_RECONNECT_MS;
    config.outbox.limit = 4096;
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

esp_err_t mqtt_send(const char *payload)
{
    int msg_id;

    if (payload == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (client == NULL || !atomic_load(&connected)) {
        return ESP_ERR_INVALID_STATE;
    }
    /* store=true: without it esp-mqtt refuses to enqueue QoS 0 messages (returns -1). */
    msg_id = esp_mqtt_client_enqueue(client, CONFIG_LOGGER_MQTT_TOPIC, payload,
                                      (int)strlen(payload), CONFIG_LOGGER_MQTT_QOS, 0, true);
    if (msg_id < 0) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
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
}

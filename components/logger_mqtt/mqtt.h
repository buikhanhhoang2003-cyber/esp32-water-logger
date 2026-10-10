#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/* Starts Wi-Fi and the MQTT client. device_id is the MQTT client ID and names the retained
 * status topic <status topic>/<device_id>: {"online":true} on connect, {"online":false} as
 * the last will. Without a broker URI nothing is started and publishing fails. */
esp_err_t mqtt_init(const char *device_id);
bool mqtt_is_connected(void);

/* QoS 1/2: returns ESP_OK only once the broker acknowledged the message (PUBACK/PUBCOMP),
 * ESP_ERR_TIMEOUT when it did not within timeout_ms. QoS 0: ESP_OK once written to the
 * socket. Call from one task at a time. */
esp_err_t mqtt_publish_acked(const char *topic, const char *payload, int qos, uint32_t timeout_ms);
/* Fire and forget. */
esp_err_t mqtt_publish(const char *topic, const char *payload, int qos, bool retain);

/* Signal strength of the access point; false while Wi-Fi is not connected. */
bool mqtt_wifi_rssi(int *rssi);
void mqtt_deinit(void);

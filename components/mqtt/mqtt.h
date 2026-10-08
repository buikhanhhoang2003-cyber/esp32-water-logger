#pragma once

#include "esp_err.h"

esp_err_t mqtt_init(void);
esp_err_t mqtt_send(const char *payload);
void mqtt_deinit(void);

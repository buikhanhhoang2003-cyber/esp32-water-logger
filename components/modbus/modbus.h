#pragma once

#include <stdint.h>

#include "esp_err.h"

esp_err_t modbus_init(void);
esp_err_t modbus_read(uint8_t slave, uint8_t func, uint16_t addr, uint16_t count, uint16_t *data);
void modbus_deinit(void);

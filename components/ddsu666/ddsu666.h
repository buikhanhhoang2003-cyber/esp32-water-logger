#pragma once

#include <stdint.h>

#include "esp_err.h"
#include "telemetry.h"

/* Reads voltage, current, active power and imported energy from a CHINT DDSU666
 * single-phase meter: registers 2000H-2005H (U V, I A, P kW) and 4000H (Ep kWh),
 * IEEE-754 floats with the high word first. Fills `reading` including its status
 * ("ok", "timeout", "invalid_response", "rejected" or "error"); values that could
 * not be read are marked invalid. Returns the first Modbus error, ESP_OK otherwise. */
esp_err_t ddsu666_read(uint8_t slave, electric_reading_t *reading);

#include "ddsu666.h"

#include <math.h>
#include <string.h>

#include "modbus.h"

#define READ_HOLDING_REGISTERS 0x03
#define REG_INSTANT 0x2000        /* U (V), I (A), P (kW): three consecutive floats */
#define REG_INSTANT_COUNT 6
#define REG_IMPORT_ENERGY 0x4000  /* Ep (kWh) */

static float to_float(const uint16_t words[2])
{
    uint32_t bits = ((uint32_t)words[0] << 16) | words[1];
    float value;

    memcpy(&value, &bits, sizeof(value));
    return value;
}

/* The meter sends single-precision floats; round them so the JSON carries the meter's
 * resolution instead of float noise such as 229.60000610351562. */
static measurement_t measurement(float raw, double scale, int decimals)
{
    double factor = pow(10.0, decimals);
    measurement_t result = {0};

    if (isfinite(raw)) {
        result.valid = true;
        result.value = round((double)raw * scale * factor) / factor;
    }
    return result;
}

static const char *status_of(esp_err_t err)
{
    switch (err) {
    case ESP_OK:
        return "ok";
    case ESP_ERR_TIMEOUT:
        return "timeout";
    case ESP_ERR_INVALID_RESPONSE:
        return "invalid_response";
    case ESP_ERR_NOT_SUPPORTED:
        return "rejected";
    default:
        return "error";
    }
}

esp_err_t ddsu666_read(uint8_t slave, electric_reading_t *reading)
{
    uint16_t instant[REG_INSTANT_COUNT];
    uint16_t energy[2];
    esp_err_t err;

    reading->voltage_v = (measurement_t){0};
    reading->current_a = (measurement_t){0};
    reading->power_w = (measurement_t){0};
    reading->energy_kwh = (measurement_t){0};

    err = modbus_read(slave, READ_HOLDING_REGISTERS, REG_INSTANT, REG_INSTANT_COUNT, instant);
    if (err == ESP_OK) {
        reading->voltage_v = measurement(to_float(&instant[0]), 1.0, 2);
        reading->current_a = measurement(to_float(&instant[2]), 1.0, 3);
        reading->power_w = measurement(to_float(&instant[4]), 1000.0, 1);  /* meter reports kW */
        err = modbus_read(slave, READ_HOLDING_REGISTERS, REG_IMPORT_ENERGY, 2, energy);
        if (err == ESP_OK) {
            reading->energy_kwh = measurement(to_float(energy), 1.0, 2);
        }
    }
    reading->status = status_of(err);
    return err;
}

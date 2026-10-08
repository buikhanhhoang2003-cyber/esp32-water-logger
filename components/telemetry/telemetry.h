#pragma once

#include <stdbool.h>
#include <stddef.h>

typedef struct {
    bool valid;
    double value;
} measurement_t;

typedef struct {
    measurement_t voltage_v;
    measurement_t current_a;
    measurement_t power_w;
    measurement_t energy_kwh;
    const char *status;
} electric_reading_t;

typedef struct {
    measurement_t total_m3;
    measurement_t flow_m3h;
    const char *status;
} water_reading_t;

typedef struct {
    electric_reading_t electric;
    water_reading_t water;
} room_reading_t;

/* Caller releases the returned string with telemetry_free(). */
char *telemetry_serialize(const char *building, const room_reading_t readings[],
                          size_t count, size_t max_bytes);
void telemetry_free(char *payload);

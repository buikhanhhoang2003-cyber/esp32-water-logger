#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "meter.h"

/* "2026-10-10T04:44:10Z" plus the terminating NUL. */
#define TELEMETRY_TS_LEN 21

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

/* Identifies one record; the server drops duplicates by (device_id, seq). */
typedef struct {
    const char *device_id;
    const char *building;
    uint32_t seq;
    uint32_t boot;
    int64_t uptime_s;
    const char *ts;       /* ISO-8601 UTC time of the measurement; NULL while the clock is not set */
} telemetry_meta_t;

typedef struct {
    const char *device_id;
    const char *building;
    const char *firmware;
    const char *ts;
    const char *reset_reason;
    uint32_t boot;
    int64_t uptime_s;
    bool rssi_valid;
    int rssi;
    uint32_t free_heap;
    uint32_t min_free_heap;
    uint32_t queue_pending;
    uint32_t queue_dropped;
} telemetry_heartbeat_t;

void telemetry_format_time(int64_t epoch_s, char out[TELEMETRY_TS_LEN]);

/* Callers release every returned string with telemetry_free(). NULL on error or when larger
 * than max_bytes. */
char *telemetry_serialize(const telemetry_meta_t *meta, const room_reading_t readings[],
                          size_t count, size_t max_bytes);
char *telemetry_heartbeat(const telemetry_heartbeat_t *heartbeat, const room_address_t rooms[],
                          const room_reading_t readings[], size_t count, size_t max_bytes);

/* A record queued before the clock was set carries "ts": null. If it was made during the
 * current boot, its measurement time follows from its uptime: returns the record with "ts"
 * filled in, or NULL when nothing can or needs to be changed. */
char *telemetry_fill_ts(const char *record, uint32_t boot, int64_t uptime_now_s, int64_t now_epoch_s,
                        size_t max_bytes);

void telemetry_free(char *payload);

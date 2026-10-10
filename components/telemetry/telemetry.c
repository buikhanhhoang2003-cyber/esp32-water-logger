#include "telemetry.h"

#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "cJSON.h"

static bool add_measurement(cJSON *object, const char *key, measurement_t reading)
{
    cJSON *value;

    if (reading.valid && isfinite(reading.value)) {
        value = cJSON_CreateNumber(reading.value);
    } else {
        value = cJSON_CreateNull();
    }
    if (value == NULL) {
        return false;
    }
    if (!cJSON_AddItemToObject(object, key, value)) {
        cJSON_Delete(value);
        return false;
    }
    return true;
}

static bool add_room(cJSON *rooms, const room_reading_t *reading, size_t index)
{
    char key[16];
    cJSON *room;
    cJSON *water;
    cJSON *electric;

    if (reading->water.status == NULL || reading->electric.status == NULL) {
        return false;
    }
    snprintf(key, sizeof(key), "room_%u", (unsigned)(index + 1));
    room = cJSON_AddObjectToObject(rooms, key);
    if (room == NULL) {
        return false;
    }
    water = cJSON_AddObjectToObject(room, "water");
    electric = cJSON_AddObjectToObject(room, "electric");
    if (water == NULL || electric == NULL) {
        return false;
    }
    if (cJSON_AddStringToObject(water, "status", reading->water.status) == NULL ||
        cJSON_AddStringToObject(electric, "status", reading->electric.status) == NULL) {
        return false;
    }
    if (!add_measurement(water, "total_m3", reading->water.total_m3) ||
        !add_measurement(water, "flow_m3h", reading->water.flow_m3h)) {
        return false;
    }
    return add_measurement(electric, "voltage_v", reading->electric.voltage_v) &&
           add_measurement(electric, "current_a", reading->electric.current_a) &&
           add_measurement(electric, "power_w", reading->electric.power_w) &&
           add_measurement(electric, "energy_kwh", reading->electric.energy_kwh);
}

static bool add_string_or_null(cJSON *object, const char *key, const char *value)
{
    return (value != NULL ? cJSON_AddStringToObject(object, key, value) : cJSON_AddNullToObject(object, key)) != NULL;
}

/* Prints and frees `root`; NULL when printing fails or the text exceeds max_bytes. */
static char *finish(cJSON *root, size_t max_bytes)
{
    char *payload = cJSON_PrintUnformatted(root);

    cJSON_Delete(root);
    if (payload != NULL && strlen(payload) > max_bytes) {
        cJSON_free(payload);
        return NULL;
    }
    return payload;
}

void telemetry_format_time(int64_t epoch_s, char out[TELEMETRY_TS_LEN])
{
    time_t seconds = (time_t)epoch_s;
    struct tm utc;

    gmtime_r(&seconds, &utc);
    strftime(out, TELEMETRY_TS_LEN, "%Y-%m-%dT%H:%M:%SZ", &utc);
}

char *telemetry_serialize(const telemetry_meta_t *meta, const room_reading_t readings[],
                          size_t count, size_t max_bytes)
{
    cJSON *root;
    cJSON *rooms;
    bool ok;

    if (meta == NULL || meta->device_id == NULL || meta->building == NULL || meta->building[0] == '\0' ||
        readings == NULL || count == 0 || count > METER_MAX_ROOMS || max_bytes == 0) {
        return NULL;
    }
    root = cJSON_CreateObject();
    if (root == NULL) {
        return NULL;
    }
    ok = cJSON_AddStringToObject(root, "device_id", meta->device_id) != NULL &&
         cJSON_AddStringToObject(root, "building", meta->building) != NULL &&
         cJSON_AddNumberToObject(root, "seq", (double)meta->seq) != NULL &&
         cJSON_AddNumberToObject(root, "boot", (double)meta->boot) != NULL &&
         cJSON_AddNumberToObject(root, "uptime_s", (double)meta->uptime_s) != NULL &&
         add_string_or_null(root, "ts", meta->ts);
    rooms = ok ? cJSON_AddObjectToObject(root, "rooms") : NULL;
    for (size_t index = 0; rooms != NULL && index < count; ++index) {
        if (!add_room(rooms, &readings[index], index)) {
            rooms = NULL;
        }
    }
    if (rooms == NULL) {
        cJSON_Delete(root);
        return NULL;
    }
    return finish(root, max_bytes);
}

static bool add_meter_status(cJSON *meters, unsigned room, const char *type, unsigned slave, const char *status)
{
    cJSON *meter = cJSON_CreateObject();

    if (meter == NULL) {
        return false;
    }
    if (cJSON_AddNumberToObject(meter, "room", room) == NULL ||
        cJSON_AddStringToObject(meter, "type", type) == NULL ||
        cJSON_AddNumberToObject(meter, "slave", slave) == NULL ||
        cJSON_AddStringToObject(meter, "status", status != NULL ? status : "unknown") == NULL ||
        !cJSON_AddItemToArray(meters, meter)) {
        cJSON_Delete(meter);
        return false;
    }
    return true;
}

char *telemetry_heartbeat(const telemetry_heartbeat_t *heartbeat, const room_address_t rooms[],
                          const room_reading_t readings[], size_t count, size_t max_bytes)
{
    cJSON *root;
    cJSON *meters;
    bool ok;

    if (heartbeat == NULL || heartbeat->device_id == NULL || rooms == NULL || readings == NULL ||
        count > METER_MAX_ROOMS || max_bytes == 0) {
        return NULL;
    }
    root = cJSON_CreateObject();
    if (root == NULL) {
        return NULL;
    }
    ok = cJSON_AddStringToObject(root, "device_id", heartbeat->device_id) != NULL &&
         add_string_or_null(root, "building", heartbeat->building) &&
         add_string_or_null(root, "firmware", heartbeat->firmware) &&
         add_string_or_null(root, "ts", heartbeat->ts) &&
         cJSON_AddNumberToObject(root, "uptime_s", (double)heartbeat->uptime_s) != NULL &&
         cJSON_AddNumberToObject(root, "boot", (double)heartbeat->boot) != NULL &&
         add_string_or_null(root, "reset_reason", heartbeat->reset_reason) &&
         (heartbeat->rssi_valid ? cJSON_AddNumberToObject(root, "rssi", heartbeat->rssi)
                                : cJSON_AddNullToObject(root, "rssi")) != NULL &&
         cJSON_AddNumberToObject(root, "free_heap", heartbeat->free_heap) != NULL &&
         cJSON_AddNumberToObject(root, "min_free_heap", heartbeat->min_free_heap) != NULL &&
         cJSON_AddNumberToObject(root, "queue_pending", heartbeat->queue_pending) != NULL &&
         cJSON_AddNumberToObject(root, "queue_dropped", heartbeat->queue_dropped) != NULL;
    meters = ok ? cJSON_AddArrayToObject(root, "meters") : NULL;
    for (size_t index = 0; meters != NULL && index < count; ++index) {
        unsigned room = (unsigned)(index + 1);
        if (!add_meter_status(meters, room, "electric", rooms[index].electric_slave, readings[index].electric.status) ||
            !add_meter_status(meters, room, "water", rooms[index].water_slave, readings[index].water.status)) {
            meters = NULL;
        }
    }
    if (meters == NULL) {
        cJSON_Delete(root);
        return NULL;
    }
    return finish(root, max_bytes);
}

char *telemetry_fill_ts(const char *record, uint32_t boot, int64_t uptime_now_s, int64_t now_epoch_s,
                        size_t max_bytes)
{
    char ts[TELEMETRY_TS_LEN];
    cJSON *root;
    cJSON *current;
    cJSON *record_boot;
    cJSON *record_uptime;
    cJSON *replacement;

    /* Cheap pre-check: most records already carry a time and need no parsing. */
    if (record == NULL || strstr(record, "\"ts\":null") == NULL) {
        return NULL;
    }
    root = cJSON_Parse(record);
    if (root == NULL) {
        return NULL;
    }
    current = cJSON_GetObjectItemCaseSensitive(root, "ts");
    record_boot = cJSON_GetObjectItemCaseSensitive(root, "boot");
    record_uptime = cJSON_GetObjectItemCaseSensitive(root, "uptime_s");
    if (!cJSON_IsNull(current) || !cJSON_IsNumber(record_boot) || !cJSON_IsNumber(record_uptime) ||
        (uint32_t)record_boot->valuedouble != boot || record_uptime->valuedouble > (double)uptime_now_s) {
        cJSON_Delete(root);
        return NULL;
    }
    telemetry_format_time(now_epoch_s - (uptime_now_s - (int64_t)record_uptime->valuedouble), ts);
    replacement = cJSON_CreateString(ts);
    if (replacement == NULL || !cJSON_ReplaceItemInObjectCaseSensitive(root, "ts", replacement)) {
        cJSON_Delete(replacement);
        cJSON_Delete(root);
        return NULL;
    }
    return finish(root, max_bytes);
}

void telemetry_free(char *payload)
{
    cJSON_free(payload);
}

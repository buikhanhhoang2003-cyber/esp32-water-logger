#include "telemetry.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "meter.h"

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

char *telemetry_serialize(const char *building, const room_reading_t readings[],
                          size_t count, size_t max_bytes)
{
    cJSON *root;
    cJSON *rooms;
    char *payload;

    if (building == NULL || building[0] == '\0' || readings == NULL ||
        count == 0 || count > METER_MAX_ROOMS || max_bytes == 0) {
        return NULL;
    }
    root = cJSON_CreateObject();
    if (root == NULL) {
        return NULL;
    }
    rooms = cJSON_AddObjectToObject(root, building);
    if (rooms == NULL) {
        cJSON_Delete(root);
        return NULL;
    }
    for (size_t index = 0; index < count; ++index) {
        if (!add_room(rooms, &readings[index], index)) {
            cJSON_Delete(root);
            return NULL;
        }
    }
    payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload != NULL && strlen(payload) > max_bytes) {
        cJSON_free(payload);
        return NULL;
    }
    return payload;
}

void telemetry_free(char *payload)
{
    cJSON_free(payload);
}

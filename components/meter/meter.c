#include "meter.h"

static bool parse_id(const char **text, bool used[248], uint8_t *id)
{
    unsigned value = 0;
    const char *cursor = *text;

    if (*cursor < '0' || *cursor > '9') {
        return false;
    }
    while (*cursor >= '0' && *cursor <= '9') {
        value = value * 10 + (unsigned)(*cursor - '0');
        if (value > 247) {
            return false;
        }
        ++cursor;
    }
    if (value == 0 || used[value]) {
        return false;
    }
    used[value] = true;
    *id = (uint8_t)value;
    *text = cursor;
    return true;
}

bool meter_parse_rooms(const char *text, room_address_t rooms[],
                       size_t capacity, size_t *count)
{
    bool used[248] = {false};
    size_t parsed = 0;

    if (count == NULL || text == NULL || rooms == NULL || capacity == 0 || *text == '\0') {
        return false;
    }
    *count = 0;
    while (*text != '\0') {
        room_address_t room = {0};
        if (parsed == capacity || !parse_id(&text, used, &room.electric_slave)) {
            return false;
        }
        if (*text != ':') {
            return false;
        }
        ++text;
        if (!parse_id(&text, used, &room.water_slave)) {
            return false;
        }
        rooms[parsed++] = room;
        if (*text == '\0') {
            *count = parsed;
            return true;
        }
        if (*text != ',' || text[1] == '\0') {
            return false;
        }
        ++text;
    }
    return false;
}

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define METER_MAX_ROOMS 16

typedef struct {
    uint8_t electric_slave;
    uint8_t water_slave;
} room_address_t;

bool meter_parse_rooms(const char *text, room_address_t rooms[], size_t capacity, size_t *count);

#include <assert.h>
#include <stddef.h>

#include "components/meter/meter.h"

int main(void)
{
    room_address_t rooms[METER_MAX_ROOMS] = {0};
    size_t count = 0;
    assert(meter_parse_rooms("1:2,3:4", rooms, METER_MAX_ROOMS, &count));
    assert(count == 2 && rooms[0].electric_slave == 1 && rooms[1].water_slave == 4);
    assert(meter_parse_rooms("247:246", rooms, METER_MAX_ROOMS, &count) && count == 1);
    assert(!meter_parse_rooms("", rooms, METER_MAX_ROOMS, &count));
    assert(!meter_parse_rooms("1:1", rooms, METER_MAX_ROOMS, &count));
    assert(!meter_parse_rooms("1:2,2:3", rooms, METER_MAX_ROOMS, &count));
    assert(!meter_parse_rooms("0:2", rooms, METER_MAX_ROOMS, &count));
    assert(!meter_parse_rooms("248:2", rooms, METER_MAX_ROOMS, &count));
    assert(!meter_parse_rooms("1:2,", rooms, METER_MAX_ROOMS, &count));
    assert(!meter_parse_rooms("1:2x", rooms, METER_MAX_ROOMS, &count));
    assert(!meter_parse_rooms("1:2", rooms, 0, &count));
    assert(!meter_parse_rooms("1:2", rooms, METER_MAX_ROOMS, NULL));
    return 0;
}

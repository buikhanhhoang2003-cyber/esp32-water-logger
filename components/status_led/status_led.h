#pragma once

#include <stdint.h>

#include "esp_err.h"

/* Three status LEDs, so a technician can read the logger's state at a glance.
 *
 * SYSTEM (GPIO5)    on           starting: until the first read cycle has finished
 *                   double blink every 2 s ("heartbeat"): read cycles keep completing
 *                   fast blink   fault: bad configuration, RS485 init failed, or no read cycle
 *                                finished for a long time (main loop stuck)
 * NETWORK (GPIO12)  off          no broker configured (offline logger)
 *                   slow blink   connecting to Wi-Fi
 *                   fast blink   Wi-Fi up, broker not reachable or login refused
 *                   on           connected to the broker; goes dark briefly for every record
 *                                the broker acknowledges
 * METERS (GPIO13)   short flash  after each read cycle in which every configured meter answered
 *                   slow blink   some meters failed in the last cycle
 *                   on           every configured meter failed (RS485 wiring, address, baud)
 *                   off          nothing read yet
 *
 * At start all three LEDs light for half a second (lamp test). */

typedef enum {
    STATUS_NET_OFFLINE,          /* no broker configured */
    STATUS_NET_WIFI_CONNECTING,
    STATUS_NET_BROKER_CONNECTING,
    STATUS_NET_ONLINE,
} status_net_t;

/* network_probe is called from the LED task a few times per second and must be cheap.
 * A cycle that has not finished stall_ms after the previous one counts as a fault. */
esp_err_t status_led_init(status_net_t (*network_probe)(void), uint32_t stall_ms);
/* Unrecoverable until reset: the logger stopped. */
void status_led_fault(void);
/* End of a read cycle: meters that answered and meters that failed (unconfigured ones excluded). */
void status_led_cycle(unsigned meters_ok, unsigned meters_failed);
/* The broker acknowledged a record. */
void status_led_sent(void);

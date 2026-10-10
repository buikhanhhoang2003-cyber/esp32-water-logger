#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* Persistent FIFO of telemetry records in the "queue" NVS partition.
 *
 * Every record gets a sequence number that keeps increasing across reboots, so the
 * server can drop duplicates by (device_id, seq). A record leaves the queue only after
 * record_queue_pop(), i.e. once the broker acknowledged it. When the queue is full the
 * oldest record is overwritten. Not thread-safe: use it from one task. */

esp_err_t record_queue_init(uint32_t capacity);
bool record_queue_ready(void);

/* Sequence number the next pushed record will get. */
uint32_t record_queue_next_seq(void);
/* Boot counter, incremented by every record_queue_init(). */
uint32_t record_queue_boot(void);
uint32_t record_queue_pending(void);
/* Records overwritten or lost since boot because the queue was full. */
uint32_t record_queue_dropped(void);

esp_err_t record_queue_push(const char *record);
/* Copies the oldest record (NUL-terminated) into `buffer`; ESP_ERR_NOT_FOUND when empty. */
esp_err_t record_queue_peek(char *buffer, size_t size, uint32_t *seq);
esp_err_t record_queue_pop(void);

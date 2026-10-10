#include "record_queue.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

#define PARTITION "queue"
#define NAMESPACE "q"

static const char *TAG = "QUEUE";
static nvs_handle_t handle;
static bool ready;
static uint32_t capacity;
static uint32_t head;     /* seq of the oldest record not acknowledged yet */
static uint32_t tail;     /* seq the next record gets */
static uint32_t boot;
static uint32_t dropped;

/* Ring slots are keyed by seq modulo capacity: "r0" .. "r<capacity-1>". */
static void slot_key(uint32_t seq, char key[16])
{
    snprintf(key, 16, "r%lu", (unsigned long)(seq % capacity));
}

static uint32_t load_u32(const char *name)
{
    uint32_t value = 0;

    nvs_get_u32(handle, name, &value);  /* missing on a fresh partition: stays 0 */
    return value;
}

static void drop_oldest(void)
{
    char key[16];

    slot_key(head, key);
    nvs_erase_key(handle, key);  /* already gone is fine */
    ++head;
    ++dropped;
    nvs_set_u32(handle, "head", head);
}

esp_err_t record_queue_init(uint32_t slots)
{
    esp_err_t err;

    if (ready || slots == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    err = nvs_flash_init_partition(PARTITION);
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "Formatting the queue partition (%s)", esp_err_to_name(err));
        err = nvs_flash_erase_partition(PARTITION);
        if (err == ESP_OK) {
            err = nvs_flash_init_partition(PARTITION);
        }
    }
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open_from_partition(PARTITION, NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    capacity = slots;
    head = load_u32("head");
    tail = load_u32("tail");
    boot = load_u32("boot") + 1;
    if (load_u32("cap") != capacity || tail - head > capacity) {
        /* Slot keys depend on the capacity: records stored under another capacity cannot be found. */
        if (tail != head) {
            ESP_LOGW(TAG, "Queue capacity changed: %lu pending records discarded", (unsigned long)(tail - head));
        }
        nvs_erase_all(handle);
        head = tail;
        nvs_set_u32(handle, "head", head);
        nvs_set_u32(handle, "tail", tail);
        nvs_set_u32(handle, "cap", capacity);
    }
    nvs_set_u32(handle, "boot", boot);
    err = nvs_commit(handle);
    if (err != ESP_OK) {
        nvs_close(handle);
        return err;
    }
    ready = true;
    return ESP_OK;
}

bool record_queue_ready(void)
{
    return ready;
}

uint32_t record_queue_next_seq(void)
{
    return tail;
}

uint32_t record_queue_boot(void)
{
    return boot;
}

uint32_t record_queue_pending(void)
{
    return tail - head;
}

uint32_t record_queue_dropped(void)
{
    return dropped;
}

esp_err_t record_queue_push(const char *record)
{
    char key[16];
    esp_err_t err;
    esp_err_t commit;

    if (!ready || record == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (tail - head >= capacity) {
        drop_oldest();
    }
    /* Write the record before advancing tail: a crash in between loses only this record. */
    slot_key(tail, key);
    while ((err = nvs_set_blob(handle, key, record, strlen(record) + 1)) == ESP_ERR_NVS_NOT_ENOUGH_SPACE &&
           head != tail) {
        drop_oldest();
    }
    if (err == ESP_OK) {
        ++tail;
        err = nvs_set_u32(handle, "tail", tail);
    }
    commit = nvs_commit(handle);
    return err != ESP_OK ? err : commit;
}

esp_err_t record_queue_peek(char *buffer, size_t size, uint32_t *seq)
{
    char key[16];

    if (!ready || buffer == NULL || size == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    while (head != tail) {
        size_t length = size;
        esp_err_t err;

        slot_key(head, key);
        err = nvs_get_blob(handle, key, buffer, &length);
        if (err == ESP_OK) {
            buffer[length > 0 ? length - 1 : 0] = '\0';
            if (seq != NULL) {
                *seq = head;
            }
            return ESP_OK;
        }
        if (err != ESP_ERR_NVS_NOT_FOUND && err != ESP_ERR_NVS_INVALID_LENGTH) {
            return err;
        }
        /* Missing (power cut between erase and head update) or larger than any valid record: skip it. */
        ESP_LOGW(TAG, "Record %lu unreadable (%s), skipped", (unsigned long)head, esp_err_to_name(err));
        nvs_erase_key(handle, key);
        ++head;
        nvs_set_u32(handle, "head", head);
        nvs_commit(handle);
    }
    return ESP_ERR_NOT_FOUND;
}

esp_err_t record_queue_pop(void)
{
    char key[16];
    esp_err_t err;

    if (!ready || head == tail) {
        return ESP_ERR_INVALID_STATE;
    }
    slot_key(head, key);
    nvs_erase_key(handle, key);
    ++head;
    err = nvs_set_u32(handle, "head", head);
    return err == ESP_OK ? nvs_commit(handle) : err;
}

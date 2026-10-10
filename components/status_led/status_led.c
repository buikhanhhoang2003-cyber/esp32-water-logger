#include "status_led.h"

#include <stdatomic.h>
#include <stdbool.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

/* A pattern is one bit per 100 ms slot over a 2 s period, slot 0 = bit 0. */
#define TICK_MS 100
#define SLOTS 20
#define PATTERN_OFF 0x00000u
#define PATTERN_ON 0xFFFFFu
#define PATTERN_HEARTBEAT 0x00005u   /* 100 ms on, 100 off, 100 on, then dark */
#define PATTERN_SLOW 0x7C1Fu         /* 500 on / 500 off: 1 Hz */
#define PATTERN_FAST 0x55555u        /* 100 on / 100 off: 5 Hz */
#define PATTERN_DOUBLE_RATE 0x6318Cu /* 200 on / 300 off: 2 Hz */
#if CONFIG_LOGGER_LED_ACTIVE_LOW
#define LIT_LEVEL 0
#else
#define LIT_LEVEL 1
#endif
#define LAMP_TEST_MS 500
#define SENT_DARK_TICKS 1
#define CYCLE_FLASH_TICKS 2

enum { LED_SYSTEM, LED_NETWORK, LED_METERS, LED_COUNT };

static const char *TAG = "LED";
static const int pins[LED_COUNT] = {
    CONFIG_LOGGER_LED_SYSTEM_GPIO, CONFIG_LOGGER_LED_NETWORK_GPIO, CONFIG_LOGGER_LED_METER_GPIO,
};
static status_net_t (*probe)(void);
static int64_t stall_us;
static int64_t started_us;
static atomic_bool fault;
static atomic_uint cycles;
static _Atomic int64_t last_cycle_us;
static atomic_uint meters_ok;
static atomic_uint meters_failed;
static atomic_uint sent_pending;    /* acknowledgements not shown yet */
static atomic_uint cycle_pending;   /* cycle flashes not shown yet */

static void set_led(int led, bool lit)
{
    if (pins[led] >= 0) {
        gpio_set_level((gpio_num_t)pins[led], lit ? LIT_LEVEL : !LIT_LEVEL);
    }
}

static uint32_t system_pattern(int64_t now_us)
{
    if (atomic_load(&fault)) {
        return PATTERN_FAST;
    }
    int64_t since = atomic_load(&cycles) == 0 ? started_us : atomic_load(&last_cycle_us);
    if (now_us - since > stall_us) {
        return PATTERN_FAST;
    }
    return atomic_load(&cycles) == 0 ? PATTERN_ON : PATTERN_HEARTBEAT;
}

static uint32_t network_pattern(void)
{
    switch (probe != NULL ? probe() : STATUS_NET_OFFLINE) {
    case STATUS_NET_WIFI_CONNECTING:
        return PATTERN_SLOW;
    case STATUS_NET_BROKER_CONNECTING:
        return PATTERN_DOUBLE_RATE;
    case STATUS_NET_ONLINE:
        return PATTERN_ON;
    default:
        return PATTERN_OFF;
    }
}

static uint32_t meter_pattern(void)
{
    unsigned ok = atomic_load(&meters_ok);
    unsigned failed = atomic_load(&meters_failed);

    if (failed == 0) {
        return PATTERN_OFF;    /* all good: only the flash after each cycle */
    }
    return ok == 0 ? PATTERN_ON : PATTERN_SLOW;
}

static void led_task(void *arg)
{
    TickType_t wake = xTaskGetTickCount();
    unsigned dark_ticks = 0;
    unsigned flash_ticks = 0;

    (void)arg;
    for (unsigned tick = 0;; ++tick) {
        int64_t now_us = esp_timer_get_time();
        unsigned slot = tick % SLOTS;
        bool fatal = atomic_load(&fault);

        if (dark_ticks == 0 && atomic_load(&sent_pending) > 0) {
            atomic_fetch_sub(&sent_pending, 1);
            dark_ticks = SENT_DARK_TICKS;
        }
        if (flash_ticks == 0 && atomic_load(&cycle_pending) > 0) {
            atomic_fetch_sub(&cycle_pending, 1);
            flash_ticks = CYCLE_FLASH_TICKS;
        }
        bool network = !fatal && ((network_pattern() >> slot) & 1u);
        bool meters = !fatal && ((meter_pattern() >> slot) & 1u);
        if (dark_ticks > 0) {
            network = false;   /* record acknowledged: blink off */
            --dark_ticks;
        }
        if (flash_ticks > 0) {
            meters = !meters;  /* cycle finished: flash */
            --flash_ticks;
        }
        set_led(LED_SYSTEM, (system_pattern(now_us) >> slot) & 1u);
        set_led(LED_NETWORK, network);
        set_led(LED_METERS, meters);
        vTaskDelayUntil(&wake, pdMS_TO_TICKS(TICK_MS));
    }
}

esp_err_t status_led_init(status_net_t (*network_probe)(void), uint32_t stall_ms)
{
    probe = network_probe;
    stall_us = (int64_t)stall_ms * 1000;
    started_us = esp_timer_get_time();
    for (int led = 0; led < LED_COUNT; ++led) {
        if (pins[led] < 0) {
            continue;
        }
        gpio_reset_pin((gpio_num_t)pins[led]);
        esp_err_t err = gpio_set_direction((gpio_num_t)pins[led], GPIO_MODE_OUTPUT);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "GPIO%d unusable for a LED: %s", pins[led], esp_err_to_name(err));
            return err;
        }
        set_led(led, true);
    }
    vTaskDelay(pdMS_TO_TICKS(LAMP_TEST_MS));
    for (int led = 0; led < LED_COUNT; ++led) {
        set_led(led, false);
    }
    if (xTaskCreate(led_task, "status_led", 2048, NULL, tskIDLE_PRIORITY + 1, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void status_led_fault(void)
{
    atomic_store(&fault, true);
}

void status_led_cycle(unsigned ok, unsigned failed)
{
    atomic_store(&meters_ok, ok);
    atomic_store(&meters_failed, failed);
    atomic_store(&last_cycle_us, esp_timer_get_time());
    atomic_fetch_add(&cycles, 1);
    if (ok > 0 && failed == 0) {
        atomic_store(&cycle_pending, 1);
    }
}

void status_led_sent(void)
{
    /* A backlog drains many records per second: one blink per tick at most, a few queued. */
    if (atomic_load(&sent_pending) < 3) {
        atomic_fetch_add(&sent_pending, 1);
    }
}

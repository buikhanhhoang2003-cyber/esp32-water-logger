/* USB <-> RS485 bridge for the DDSU666 PC tool.
 *
 * The PC sends complete Modbus RTU frames as hex text over the USB serial port
 * (UART0); this firmware puts them on the RS485 bus (UART2, TX16/RX17 by default)
 * and returns whatever the meter answers. Protocol: see bridge_proto.h.
 */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "bridge_proto.h"

#define HOST_PORT UART_NUM_0
#define HOST_BAUD 115200
#define HOST_RX_BUFFER 2048
#define BUS_PORT ((uart_port_t)CONFIG_BRIDGE_UART_PORT)
#define BUS_RX_BUFFER 1024
#define BUS_EVENT_DEPTH 32
/* The UART reports a frame end after this many idle characters (Modbus: 3.5). */
#define BUS_IDLE_CHARS 4

static const char *TAG = "BRIDGE";

static QueueHandle_t bus_events;
static esp_err_t bus_status = ESP_FAIL;
static bridge_line_cfg_t bus_cfg = {CONFIG_BRIDGE_DEFAULT_BAUD, 'N', 1};

static bridge_cmd_t command;
static uint8_t reply[BRIDGE_MAX_FRAME];
static char line[BRIDGE_MAX_LINE + 1];
static char hex[2 * BRIDGE_MAX_FRAME + 1];
static char out[2 * BRIDGE_MAX_FRAME + 64];

typedef struct {
    size_t len;
    bool parity_error;
    bool frame_error;
    bool overflow;
} bus_result_t;

static void host_send(const char *format, ...)
{
    va_list args;
    int len;

    va_start(args, format);
    len = vsnprintf(out, sizeof(out), format, args);
    va_end(args);
    if (len < 0) {
        return;
    }
    if ((size_t)len >= sizeof(out)) {
        len = sizeof(out) - 1;
    }
    uart_write_bytes(HOST_PORT, out, (size_t)len);
}

static TickType_t ms_to_ticks(uint32_t ms)
{
    TickType_t ticks = pdMS_TO_TICKS(ms);
    return ticks > 0 ? ticks : 1;
}

static esp_err_t bus_apply(const bridge_line_cfg_t *cfg)
{
    uart_parity_t parity = cfg->parity == 'E' ? UART_PARITY_EVEN
                         : cfg->parity == 'O' ? UART_PARITY_ODD
                                              : UART_PARITY_DISABLE;

    ESP_RETURN_ON_ERROR(uart_set_baudrate(BUS_PORT, cfg->baud), TAG, "baud rate");
    ESP_RETURN_ON_ERROR(uart_set_word_length(BUS_PORT, UART_DATA_8_BITS), TAG, "data bits");
    ESP_RETURN_ON_ERROR(uart_set_parity(BUS_PORT, parity), TAG, "parity");
    ESP_RETURN_ON_ERROR(uart_set_stop_bits(BUS_PORT, cfg->stop_bits == 2 ? UART_STOP_BITS_2 : UART_STOP_BITS_1),
                        TAG, "stop bits");
    /* The RX timeout is counted in characters of the current format: set it last. */
    ESP_RETURN_ON_ERROR(uart_set_rx_timeout(BUS_PORT, BUS_IDLE_CHARS), TAG, "rx timeout");
    /* Also raise the timeout event when a frame ends exactly on a FIFO-full boundary. */
    uart_set_always_rx_timeout(BUS_PORT, true);
    uart_flush_input(BUS_PORT);
    xQueueReset(bus_events);
    bus_cfg = *cfg;
    return ESP_OK;
}

static esp_err_t bus_init(void)
{
    const uart_config_t config = {
        .baud_rate = (int)bus_cfg.baud,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    const int tx = CONFIG_BRIDGE_TX_GPIO;
    const int rx = CONFIG_BRIDGE_RX_GPIO;

    if (!GPIO_IS_VALID_OUTPUT_GPIO(tx) || !GPIO_IS_VALID_GPIO(rx) || tx == rx) {
        return ESP_ERR_INVALID_ARG;
    }
#if CONFIG_BRIDGE_DE_GPIO >= 0
    if (!GPIO_IS_VALID_OUTPUT_GPIO(CONFIG_BRIDGE_DE_GPIO) || CONFIG_BRIDGE_DE_GPIO == tx ||
        CONFIG_BRIDGE_DE_GPIO == rx) {
        return ESP_ERR_INVALID_ARG;
    }
#endif
    ESP_RETURN_ON_ERROR(uart_driver_install(BUS_PORT, BUS_RX_BUFFER, 0, BUS_EVENT_DEPTH, &bus_events, 0),
                        TAG, "driver install");
    ESP_RETURN_ON_ERROR(uart_param_config(BUS_PORT, &config), TAG, "config");
#if CONFIG_BRIDGE_DE_GPIO >= 0
    ESP_RETURN_ON_ERROR(uart_set_pin(BUS_PORT, tx, rx, CONFIG_BRIDGE_DE_GPIO, UART_PIN_NO_CHANGE), TAG, "pins");
    /* The driver raises RTS (wired to DE/RE) while transmitting. */
    ESP_RETURN_ON_ERROR(uart_set_mode(BUS_PORT, UART_MODE_RS485_HALF_DUPLEX), TAG, "rs485 mode");
#else
    ESP_RETURN_ON_ERROR(uart_set_pin(BUS_PORT, tx, rx, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE), TAG, "pins");
#endif
    return bus_apply(&bus_cfg);
}

static void bus_transact(const bridge_cmd_t *cmd, bus_result_t *result)
{
    uint32_t tx_ms = (uint32_t)((cmd->frame_len * bridge_char_time_us(&bus_cfg)) / 1000);
    TickType_t limit = ms_to_ticks(cmd->timeout_ms);
    TickType_t start;
    uart_event_t event;

    memset(result, 0, sizeof(*result));
    uart_flush_input(BUS_PORT);
    xQueueReset(bus_events);
    uart_write_bytes(BUS_PORT, cmd->frame, cmd->frame_len);
    uart_wait_tx_done(BUS_PORT, ms_to_ticks(tx_ms + 100));
    /* Transceivers that keep their receiver on while sending echo our own frame: drop it.
     * The meter cannot answer before 3.5 characters of silence, so nothing else is lost. */
    uart_flush_input(BUS_PORT);
    xQueueReset(bus_events);

    start = xTaskGetTickCount();
    for (;;) {
        TickType_t elapsed = xTaskGetTickCount() - start;
        if (elapsed >= limit || xQueueReceive(bus_events, &event, limit - elapsed) != pdTRUE) {
            break;
        }
        if (event.type == UART_DATA) {
            size_t room = sizeof(reply) - result->len;
            size_t want = event.size < room ? event.size : room;
            int got = want > 0 ? uart_read_bytes(BUS_PORT, reply + result->len, want, 0) : 0;
            if (got > 0) {
                result->len += (size_t)got;
            }
            if (want < event.size) {
                result->overflow = true;
                break;
            }
            if (bridge_frame_complete(reply, result->len, cmd->expect_len, event.timeout_flag)) {
                break;
            }
        } else if (event.type == UART_PARITY_ERR) {
            result->parity_error = true;
        } else if (event.type == UART_FRAME_ERR) {
            result->frame_error = true;
        } else if (event.type == UART_FIFO_OVF || event.type == UART_BUFFER_FULL) {
            result->overflow = true;
            break;
        }
    }
}

static void handle_line(char *text)
{
    const char *error = bridge_parse_command(text, &command);
    char format[4];

    if (error != NULL) {
        host_send("@ERR %s\n", error);
        return;
    }
    if (command.type == BRIDGE_CMD_PING) {
        bridge_format_name(&bus_cfg, format);
        host_send("@PONG RS485-BRIDGE %s port=%d tx=%d rx=%d de=%d baud=%lu fmt=%s bus=%s\n",
                  BRIDGE_VERSION, CONFIG_BRIDGE_UART_PORT, CONFIG_BRIDGE_TX_GPIO, CONFIG_BRIDGE_RX_GPIO,
                  CONFIG_BRIDGE_DE_GPIO, (unsigned long)bus_cfg.baud, format,
                  bus_status == ESP_OK ? "ok" : esp_err_to_name(bus_status));
        return;
    }
    if (bus_status != ESP_OK) {
        host_send("@ERR BUS_INIT_%s\n", esp_err_to_name(bus_status));
        return;
    }
    if (command.type == BRIDGE_CMD_CFG) {
        if (bus_apply(&command.cfg) != ESP_OK) {
            host_send("@ERR UART_CONFIG\n");
            return;
        }
        bridge_format_name(&bus_cfg, format);
        host_send("@OK baud=%lu fmt=%s\n", (unsigned long)bus_cfg.baud, format);
        return;
    }

    bus_result_t result;
    bus_transact(&command, &result);
    const char *pe = result.parity_error ? " PE" : "";
    const char *fe = result.frame_error ? " FE" : "";
    const char *ovf = result.overflow ? " OVF" : "";
    if (result.len == 0) {
        host_send("@TIMEOUT%s%s%s\n", pe, fe, ovf);
        return;
    }
    bridge_hex_encode(reply, result.len, hex);
    host_send("@RX %s%s%s%s\n", hex, pe, fe, ovf);
}

static void host_loop(void)
{
    uint8_t chunk[64];
    size_t len = 0;
    bool too_long = false;

    for (;;) {
        int got = uart_read_bytes(HOST_PORT, chunk, sizeof(chunk), ms_to_ticks(20));
        for (int i = 0; i < got; ++i) {
            char c = (char)chunk[i];
            if (c == '\r') {
                continue;
            }
            if (c == '\n') {
                if (too_long) {
                    host_send("@ERR LINE_TOO_LONG\n");
                } else if (len > 0) {
                    line[len] = '\0';
                    handle_line(line);
                }
                len = 0;
                too_long = false;
            } else if (len < BRIDGE_MAX_LINE) {
                line[len++] = c;
            } else {
                too_long = true;
            }
        }
    }
}

void app_main(void)
{
    const uart_config_t host_config = {
        .baud_rate = HOST_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    ESP_ERROR_CHECK(uart_driver_install(HOST_PORT, HOST_RX_BUFFER, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(HOST_PORT, &host_config));
    bus_status = bus_init();
    if (bus_status != ESP_OK) {
        ESP_LOGE(TAG, "RS485 UART%d init failed (TX%d RX%d DE%d): %s", CONFIG_BRIDGE_UART_PORT,
                 CONFIG_BRIDGE_TX_GPIO, CONFIG_BRIDGE_RX_GPIO, CONFIG_BRIDGE_DE_GPIO, esp_err_to_name(bus_status));
    }
    host_send("@READY RS485-BRIDGE %s\n", BRIDGE_VERSION);
    host_loop();
}

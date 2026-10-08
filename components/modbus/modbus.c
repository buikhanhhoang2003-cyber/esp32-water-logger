#include "modbus.h"

#include <stddef.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "mbcontroller.h"
#include "sdkconfig.h"
#include "soc/soc_caps.h"

static const char *TAG = "MODBUS";
static void *master;

static esp_err_t check_pins(void)
{
    if (CONFIG_LOGGER_UART_PORT >= SOC_UART_NUM) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!GPIO_IS_VALID_OUTPUT_GPIO(CONFIG_LOGGER_TX_GPIO) ||
        !GPIO_IS_VALID_GPIO(CONFIG_LOGGER_RX_GPIO) ||
        !GPIO_IS_VALID_OUTPUT_GPIO(CONFIG_LOGGER_RTS_GPIO)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (CONFIG_LOGGER_TX_GPIO == CONFIG_LOGGER_RX_GPIO ||
        CONFIG_LOGGER_TX_GPIO == CONFIG_LOGGER_RTS_GPIO ||
        CONFIG_LOGGER_RX_GPIO == CONFIG_LOGGER_RTS_GPIO) {
        return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

static mb_communication_info_t serial_config(void)
{
    mb_communication_info_t comm = {0};
    comm.ser_opts.port = CONFIG_LOGGER_UART_PORT;
    comm.ser_opts.mode = MB_RTU;
    comm.ser_opts.baudrate = CONFIG_LOGGER_BAUD;
#if CONFIG_LOGGER_PARITY_EVEN
    comm.ser_opts.parity = MB_PARITY_EVEN;
#elif CONFIG_LOGGER_PARITY_ODD
    comm.ser_opts.parity = MB_PARITY_ODD;
#else
    comm.ser_opts.parity = MB_PARITY_NONE;
#endif
    comm.ser_opts.response_tout_ms = CONFIG_LOGGER_TIMEOUT_MS;
    comm.ser_opts.data_bits = UART_DATA_8_BITS;
#if CONFIG_LOGGER_STOP_BITS_TWO
    comm.ser_opts.stop_bits = UART_STOP_BITS_2;
#else
    comm.ser_opts.stop_bits = UART_STOP_BITS_1;
#endif
    return comm;
}

esp_err_t modbus_init(void)
{
    esp_err_t err;
    mb_communication_info_t comm;

    if (master != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    err = check_pins();
    if (err != ESP_OK) {
        return err;
    }
    comm = serial_config();
    err = mbc_master_create_serial(&comm, &master);
    if (err != ESP_OK) {
        return err;
    }
    err = uart_set_pin(CONFIG_LOGGER_UART_PORT, CONFIG_LOGGER_TX_GPIO,
                       CONFIG_LOGGER_RX_GPIO, CONFIG_LOGGER_RTS_GPIO, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) {
        goto fail;
    }
    err = uart_set_mode(CONFIG_LOGGER_UART_PORT, UART_MODE_RS485_HALF_DUPLEX);
    if (err != ESP_OK) {
        goto fail;
    }
    err = mbc_master_start(master);
    if (err != ESP_OK) {
        goto fail;
    }
    ESP_LOGI(TAG, "RTU master ready, UART=%d baud=%d", CONFIG_LOGGER_UART_PORT, CONFIG_LOGGER_BAUD);
    return ESP_OK;

fail:
    modbus_deinit();
    return err;
}

esp_err_t modbus_read(uint8_t slave, uint8_t func, uint16_t addr,
                      uint16_t count, uint16_t *data)
{
    mb_param_request_t request = {0};
    esp_err_t err;
    int attempts = 0;

    if (master == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (data == NULL || slave == 0 || slave > 247 || count == 0 || count > 125) {
        return ESP_ERR_INVALID_ARG;
    }
    if (func != 0x03 && func != 0x04) {
        return ESP_ERR_INVALID_ARG;
    }
    if ((uint32_t)addr + count > 65536) {
        return ESP_ERR_INVALID_ARG;
    }
    request.slave_addr = slave;
    request.command = func;
    request.reg_start = addr;
    request.reg_size = count;
    for (int attempt = 0; attempt <= CONFIG_LOGGER_RETRIES; ++attempt) {
        attempts = attempt + 1;
        err = mbc_master_send_request(master, &request, data);
        if (err != ESP_ERR_TIMEOUT) {
            break;
        }
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "slave=%u function=%u failed after %d attempt(s): %s",
                 slave, func, attempts, esp_err_to_name(err));
    }
    return err;
}

void modbus_deinit(void)
{
    if (master != NULL) {
        esp_err_t err = mbc_master_delete(master);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Master cleanup failed: %s", esp_err_to_name(err));
        }
        master = NULL;
    }
}


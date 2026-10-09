#pragma once

/* Text protocol between the PC tool and the RS485 bridge (no ESP-IDF dependency,
 * so it can be unit-tested on the host). One command per line, replies start with '@':
 *
 *   PING                          -> @PONG RS485-BRIDGE <ver> port=2 tx=16 rx=17 de=-1 baud=9600 fmt=8N1 bus=ok
 *   CFG <baud> <8N1|8N2|8E1|...>  -> @OK baud=<baud> fmt=<fmt>
 *   TX <hex> [timeout_ms [len]]   -> @RX <hex> [PE] [FE] [OVF]   or   @TIMEOUT
 *   anything wrong                -> @ERR <reason>
 *
 * TX puts the bytes on the bus unchanged (the PC appends the Modbus CRC) and returns
 * what came back. timeout_ms counts from the end of transmission; len, when known,
 * ends the wait as soon as that many bytes (or a 5-byte exception reply) arrived.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BRIDGE_VERSION "1.0"
#define BRIDGE_MAX_FRAME 256
#define BRIDGE_MAX_LINE 600
#define BRIDGE_DEFAULT_TIMEOUT_MS 500
#define BRIDGE_MAX_TIMEOUT_MS 10000
#define BRIDGE_MIN_BAUD 1200
#define BRIDGE_MAX_BAUD 115200

typedef struct {
    uint32_t baud;
    char parity;        /* 'N', 'E' or 'O'; always 8 data bits */
    uint8_t stop_bits;  /* 1 or 2 */
} bridge_line_cfg_t;

typedef enum {
    BRIDGE_CMD_PING,
    BRIDGE_CMD_CFG,
    BRIDGE_CMD_TX,
} bridge_cmd_type_t;

typedef struct {
    bridge_cmd_type_t type;
    bridge_line_cfg_t cfg;
    uint8_t frame[BRIDGE_MAX_FRAME];
    size_t frame_len;
    uint32_t timeout_ms;
    size_t expect_len;
} bridge_cmd_t;

/* Parses `line` (modified in place). Returns NULL on success, else the reason for "@ERR <reason>". */
const char *bridge_parse_command(char *line, bridge_cmd_t *cmd);

bool bridge_parse_format(const char *text, bridge_line_cfg_t *cfg);
void bridge_format_name(const bridge_line_cfg_t *cfg, char out[4]);

/* Returns the number of bytes decoded, or -1 for odd length, bad digits or overflow. */
int bridge_hex_decode(const char *hex, uint8_t *out, size_t capacity);
/* `out` needs 2 * len + 1 bytes. */
void bridge_hex_encode(const uint8_t *data, size_t len, char *out);

/* True once `data` holds a whole Modbus answer: `expect_len` bytes, or a 5-byte
 * exception reply; with expect_len 0, once the line went idle after some data. */
bool bridge_frame_complete(const uint8_t *data, size_t len, size_t expect_len, bool line_idle);

/* Duration of one character (start + 8 data + parity + stop bits) in microseconds. */
uint32_t bridge_char_time_us(const bridge_line_cfg_t *cfg);

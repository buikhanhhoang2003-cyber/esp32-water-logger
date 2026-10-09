#include "bridge_proto.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#define MAX_ARGS 3

static int hex_digit(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

int bridge_hex_decode(const char *hex, uint8_t *out, size_t capacity)
{
    size_t len = strlen(hex);

    if (len == 0 || len % 2 != 0 || len / 2 > capacity) {
        return -1;
    }
    for (size_t i = 0; i < len / 2; ++i) {
        int high = hex_digit(hex[2 * i]);
        int low = hex_digit(hex[2 * i + 1]);
        if (high < 0 || low < 0) {
            return -1;
        }
        out[i] = (uint8_t)((high << 4) | low);
    }
    return (int)(len / 2);
}

void bridge_hex_encode(const uint8_t *data, size_t len, char *out)
{
    static const char digits[] = "0123456789ABCDEF";

    for (size_t i = 0; i < len; ++i) {
        out[2 * i] = digits[data[i] >> 4];
        out[2 * i + 1] = digits[data[i] & 0x0F];
    }
    out[2 * len] = '\0';
}

bool bridge_parse_format(const char *text, bridge_line_cfg_t *cfg)
{
    char parity;

    if (strlen(text) != 3 || text[0] != '8') {
        return false;
    }
    parity = (char)toupper((unsigned char)text[1]);
    if (parity != 'N' && parity != 'E' && parity != 'O') {
        return false;
    }
    if (text[2] != '1' && text[2] != '2') {
        return false;
    }
    cfg->parity = parity;
    cfg->stop_bits = (uint8_t)(text[2] - '0');
    return true;
}

void bridge_format_name(const bridge_line_cfg_t *cfg, char out[4])
{
    out[0] = '8';
    out[1] = cfg->parity;
    out[2] = (char)('0' + cfg->stop_bits);
    out[3] = '\0';
}

static bool parse_uint(const char *text, uint32_t min, uint32_t max, uint32_t *value)
{
    char *end;
    unsigned long parsed;

    if (!isdigit((unsigned char)text[0]) || strlen(text) > 9) {
        return false;
    }
    parsed = strtoul(text, &end, 10);
    if (*end != '\0' || parsed < min || parsed > max) {
        return false;
    }
    *value = (uint32_t)parsed;
    return true;
}

static char *next_token(char **cursor)
{
    char *start = *cursor;
    char *end;

    while (*start == ' ' || *start == '\t') {
        ++start;
    }
    if (*start == '\0') {
        *cursor = start;
        return NULL;
    }
    end = start;
    while (*end != '\0' && *end != ' ' && *end != '\t') {
        ++end;
    }
    if (*end != '\0') {
        *end++ = '\0';
    }
    *cursor = end;
    return start;
}

const char *bridge_parse_command(char *line, bridge_cmd_t *cmd)
{
    char *cursor = line;
    char *name = next_token(&cursor);
    char *args[MAX_ARGS];
    size_t argc = 0;
    char *extra;
    uint32_t value;

    if (name == NULL) {
        return "EMPTY";
    }
    for (char *c = name; *c != '\0'; ++c) {
        *c = (char)toupper((unsigned char)*c);
    }
    while (argc < MAX_ARGS && (args[argc] = next_token(&cursor)) != NULL) {
        ++argc;
    }
    extra = next_token(&cursor);

    if (strcmp(name, "PING") == 0) {
        if (argc != 0) {
            return "BAD_ARGS";
        }
        cmd->type = BRIDGE_CMD_PING;
        return NULL;
    }
    if (strcmp(name, "CFG") == 0) {
        if (argc != 2 || extra != NULL) {
            return "BAD_ARGS";
        }
        if (!parse_uint(args[0], BRIDGE_MIN_BAUD, BRIDGE_MAX_BAUD, &value)) {
            return "BAD_BAUD";
        }
        if (!bridge_parse_format(args[1], &cmd->cfg)) {
            return "BAD_FORMAT";
        }
        cmd->cfg.baud = value;
        cmd->type = BRIDGE_CMD_CFG;
        return NULL;
    }
    if (strcmp(name, "TX") == 0) {
        int len;
        if (argc < 1 || extra != NULL) {
            return "BAD_ARGS";
        }
        len = bridge_hex_decode(args[0], cmd->frame, sizeof(cmd->frame));
        if (len < 0) {
            return "BAD_HEX";
        }
        cmd->frame_len = (size_t)len;
        cmd->timeout_ms = BRIDGE_DEFAULT_TIMEOUT_MS;
        cmd->expect_len = 0;
        if (argc >= 2) {
            if (!parse_uint(args[1], 1, BRIDGE_MAX_TIMEOUT_MS, &value)) {
                return "BAD_TIMEOUT";
            }
            cmd->timeout_ms = value;
        }
        if (argc == 3) {
            if (!parse_uint(args[2], 0, BRIDGE_MAX_FRAME, &value)) {
                return "BAD_LENGTH";
            }
            cmd->expect_len = value;
        }
        cmd->type = BRIDGE_CMD_TX;
        return NULL;
    }
    return "UNKNOWN_COMMAND";
}

bool bridge_frame_complete(const uint8_t *data, size_t len, size_t expect_len, bool line_idle)
{
    if (expect_len == 0) {
        return line_idle && len > 0;
    }
    if (len >= 5 && (data[1] & 0x80) != 0) {
        return true;
    }
    return len >= expect_len;
}

uint32_t bridge_char_time_us(const bridge_line_cfg_t *cfg)
{
    uint32_t bits = 1 + 8 + (cfg->parity == 'N' ? 0 : 1) + cfg->stop_bits;

    return (bits * 1000000UL + cfg->baud - 1) / cfg->baud;
}

#include "dms_protocol.h"
#include <stddef.h>

static const uint8_t DMS1_PREFIX[4] = {'D', 'M', 'S', '1'};

static dms_state_t parse_state(const uint8_t *p, uint16_t len)
{
    if (len == 6) {
        if (p[0]=='N' && p[1]=='O' && p[2]=='R' && p[3]=='M' && p[4]=='A' && p[5]=='L')
            return DMS_STATE_NORMAL;
    } else if (len == 4) {
        if (p[0]=='Y' && p[1]=='A' && p[2]=='W' && p[3]=='N')
            return DMS_STATE_YAWN;
    } else if (len == 7) {
        if (p[0]=='F' && p[1]=='A' && p[2]=='T' && p[3]=='I' && p[4]=='G' && p[5]=='U' && p[6]=='E')
            return DMS_STATE_FATIGUE;
        if (p[0]=='U' && p[1]=='N' && p[2]=='K' && p[3]=='N' && p[4]=='O' && p[5]=='W' && p[6]=='N')
            return DMS_STATE_UNKNOWN;
    }
    return DMS_STATE_INVALID;
}

static int32_t parse_uint16(const uint8_t *p, uint16_t len, uint16_t max_val)
{
    uint32_t v = 0;
    uint16_t i;
    if (len == 0 || len > 5) return -1;
    for (i = 0; i < len; i++) {
        if (p[i] < '0' || p[i] > '9') return -1;
        v = v * 10 + (p[i] - '0');
    }
    if (v > max_val) return -1;
    return (int32_t)v;
}

dms_parsed_frame_t dms_parse(const uint8_t *data, uint16_t len)
{
    dms_parsed_frame_t result;
    result.seq = 0;
    result.state = DMS_STATE_INVALID;
    result.confidence = 0;

    if (data == NULL) return result;
    if (len < 10 || len > DMS1_FRAME_MAX_BYTES) return result;
    if (data[len-2] != '\r' || data[len-1] != '\n') return result;

    if (data[0]!=DMS1_PREFIX[0] || data[1]!=DMS1_PREFIX[1] ||
        data[2]!=DMS1_PREFIX[2] || data[3]!=DMS1_PREFIX[3])
        return result;

    if (data[4] != ',') return result;

    uint16_t pos = 5;
    uint16_t field_start;

    field_start = pos;
    while (pos < len && data[pos] != ',') pos++;
    if (pos == len || pos == field_start) return result;
    int32_t v = parse_uint16(data + field_start, pos - field_start, 65535);
    if (v < 0) return result;
    result.seq = (uint16_t)v;
    pos++;

    field_start = pos;
    while (pos < len && data[pos] != ',') pos++;
    if (pos == len || pos == field_start) return result;
    dms_state_t s = parse_state(data + field_start, pos - field_start);
    if (s == DMS_STATE_INVALID) return result;
    pos++;

    field_start = pos;
    while (pos < len && data[pos] != '\r') pos++;
    if (pos == field_start) return result;
    v = parse_uint16(data + field_start, pos - field_start, 100);
    if (v < 0) return result;
    result.confidence = (uint8_t)v;

    if (pos + 2 != len) return result;

    result.state = s;
    return result;
}

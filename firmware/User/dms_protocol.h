#ifndef __DMS_PROTOCOL_H
#define __DMS_PROTOCOL_H

#include <stdint.h>

#define DMS1_FRAME_MAX_BYTES   63
#define DMS1_STATE_MAX_LEN     8

typedef enum {
    DMS_STATE_NORMAL   = 0,
    DMS_STATE_YAWN     = 1,
    DMS_STATE_FATIGUE  = 2,
    DMS_STATE_UNKNOWN  = 3,
    DMS_STATE_BOOT     = 4,
    DMS_STATE_LINK_LOST= 5,
    DMS_STATE_INVALID  = 6
} dms_state_t;

typedef struct {
    uint16_t    seq;
    dms_state_t state;
    uint8_t     confidence;
} dms_parsed_frame_t;

dms_parsed_frame_t dms_parse(const uint8_t *data, uint16_t len);

#endif

#ifndef __DMS_APP_H
#define __DMS_APP_H

#include "dms_protocol.h"

int dms_app_init(void);

extern void dms_rx_byte_isr(uint8_t byte);

#endif

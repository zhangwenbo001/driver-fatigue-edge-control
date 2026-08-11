#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "../../正点原子工程模板/User/dms_protocol.h"
int main() {
    const char *s = "DMS1,0,NORMAL,101\r\n";
    size_t len = strlen(s);
    printf("len=%zu bytes:", len);
    for (size_t i = 0; i < len; i++) printf(" %02X", (unsigned char)s[i]);
    printf("\n");
    printf("data[%d]=%02X data[%d]=%02X\n", (int)len-2, (unsigned char)s[len-2], (int)len-1, (unsigned char)s[len-1]);
    
    dms_parsed_frame_t f = dms_parse((const uint8_t*)s, (uint16_t)len);
    printf("CONF101: state=%d seq=%d conf=%d\n", f.state, f.seq, f.confidence);
    
    /* test with explicit bytes */
    uint8_t explicit_frame[] = {0x44,0x4D,0x53,0x31,0x2C,0x30,0x2C,0x4E,0x4F,0x52,0x4D,0x41,0x4C,0x2C,0x31,0x30,0x31,0x0D,0x0A};
    f = dms_parse(explicit_frame, 19);
    printf("EXPLICIT: state=%d seq=%d conf=%d\n", f.state, f.seq, f.confidence);
    
    /* extra field */
    const char *se = "DMS1,0,NORMAL,95,EXTRA\r\n";
    len = strlen(se);
    printf("EXTRALEN=%zu\n", len);
    f = dms_parse((const uint8_t*)se, (uint16_t)len);
    printf("EXTRA: state=%d seq=%d conf=%d\n", f.state, f.seq, f.confidence);
    return 0;
}

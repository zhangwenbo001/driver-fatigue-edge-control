#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "../../正点原子工程模板/User/dms_protocol.h"

static int tests_run = 0;
static int tests_failed = 0;
static const char *current_test = "NONE";

#define DO_TEST(fn) do { fn; } while(0)

void check_valid(const char *name, const uint8_t *d, uint16_t l,
    dms_state_t es, uint16_t eq, uint8_t ec)
{
    current_test = name; tests_run++;
    dms_parsed_frame_t f = dms_parse(d, l);
    int ok = 1;
    if (f.state != es) { printf("  FAIL [%s] state=%d exp=%d (line %d)\n", name, f.state, es, __LINE__); ok=0; }
    if (f.seq != eq)   { printf("  FAIL [%s] seq=%d exp=%d (line %d)\n", name, f.seq, eq, __LINE__); ok=0; }
    if (f.confidence != ec) { printf("  FAIL [%s] conf=%d exp=%d (line %d)\n", name, f.confidence, ec, __LINE__); ok=0; }
    if (ok) printf("  PASS [%s]\n", name); else tests_failed++;
}

void check_invalid(const char *name, const uint8_t *d, uint16_t l)
{
    current_test = name; tests_run++;
    dms_parsed_frame_t f = dms_parse(d, l);
    if (f.state != DMS_STATE_INVALID) {
        printf("  FAIL [%s] state=%d expected INVALID (line %d)\n", name, f.state, __LINE__);
        tests_failed++;
    } else {
        printf("  PASS [%s]\n", name);
    }
}

#define S(str) ((const uint8_t*)(str)), ((uint16_t)strlen(str))

int main(void)
{
    printf("=== G4-01 dms_protocol.c Host Test Harness ===\n\n");

    /* 1. Four legal states */
    check_valid("DMS1,0,NORMAL,100",  S("DMS1,0,NORMAL,100\r\n"),   DMS_STATE_NORMAL,  0,     100);
    check_valid("DMS1,42,YAWN,88",    S("DMS1,42,YAWN,88\r\n"),     DMS_STATE_YAWN,    42,    88);
    check_valid("DMS1,65535,FATIGUE,50",S("DMS1,65535,FATIGUE,50\r\n"), DMS_STATE_FATIGUE, 65535, 50);
    check_valid("DMS1,100,UNKNOWN,0", S("DMS1,100,UNKNOWN,0\r\n"),  DMS_STATE_UNKNOWN, 100,   0);

    /* 2. seq 0, 32767, 32768, 65535 */
    check_valid("seq=0",      S("DMS1,0,NORMAL,50\r\n"),      DMS_STATE_NORMAL, 0,     50);
    check_valid("seq=32767",  S("DMS1,32767,NORMAL,50\r\n"),  DMS_STATE_NORMAL, 32767, 50);
    check_valid("seq=32768",  S("DMS1,32768,NORMAL,50\r\n"),  DMS_STATE_NORMAL, 32768, 50);
    check_valid("seq=65535",  S("DMS1,65535,NORMAL,50\r\n"),  DMS_STATE_NORMAL, 65535, 50);

    /* 3. confidence 0/100 */
    check_valid("conf=0",   S("DMS1,0,NORMAL,0\r\n"),   DMS_STATE_NORMAL, 0, 0);
    check_valid("conf=100", S("DMS1,0,NORMAL,100\r\n"), DMS_STATE_NORMAL, 0, 100);

    /* 4. confidence out-of-range */
    check_invalid("conf=101", S("DMS1,0,NORMAL,101\r\n"));
    check_invalid("conf=255", S("DMS1,0,NORMAL,255\r\n"));
    check_invalid("conf=-1",  S("DMS1,0,NORMAL,-1\r\n"));

    /* 5. CRLF checks */
    check_invalid("no-CR",    S("DMS1,0,NORMAL,95\n"));
    check_invalid("no-LF",    S("DMS1,0,NORMAL,95\r"));
    check_invalid("no-CRLF",  S("DMS1,0,NORMAL,95"));
    check_invalid("LFCR",     S("DMS1,0,NORMAL,95\n\r"));

    /* 6. Bad headers */
    check_invalid("XMS1",     S("XMS1,0,NORMAL,95\r\n"));
    check_invalid("dms1-lc",  S("dms1,0,NORMAL,95\r\n"));
    check_invalid("DMS2",     S("DMS2,0,NORMAL,95\r\n"));
    check_invalid("empty",    S("\r\n"));

    /* 7. Bad states */
    check_invalid("BOOT",       S("DMS1,0,BOOT,0\r\n"));
    check_invalid("LINK_LOST",  S("DMS1,0,LINK_LOST,0\r\n"));
    check_invalid("normal-lc",  S("DMS1,0,normal,95\r\n"));
    check_invalid("empty-state", S("DMS1,0,,0\r\n"));
    check_invalid("only-commas", S("DMS1,,,\r\n"));

    /* 8. Non-digit seq/conf */
    check_invalid("seq=abc",   S("DMS1,abc,NORMAL,95\r\n"));
    check_invalid("seq=65536", S("DMS1,65536,NORMAL,95\r\n"));
    check_invalid("seq=123456",S("DMS1,123456,NORMAL,95\r\n"));

    /* 9. Missing/extra fields */
    check_invalid("missing-conf", S("DMS1,0,NORMAL\r\n"));
    check_invalid("missing-state+conf", S("DMS1,0\r\n"));
    check_invalid("extra-field", S("DMS1,0,NORMAL,95,EXTRA\r\n"));

    /* 10. NULL data */
    current_test = "NULL data"; tests_run++;
    {
        dms_parsed_frame_t f = dms_parse(NULL, 20);
        if (f.state == DMS_STATE_INVALID)
            printf("  PASS [%s]\n", current_test);
        else {
            printf("  FAIL [%s] state=%d expected INVALID (line %d)\n", current_test, f.state, __LINE__);
            tests_failed++;
        }
    }

    /* 11. Non-ASCII byte in header */
    {
        uint8_t buf[32];
        memcpy(buf, "DMS1,0,NORMAL,95\r\n", 18);
        buf[2] = 0xFF;
        check_invalid("nonascii-header", buf, 18);
    }

    /* 12. Length boundary: 62, 63, 64 */
    {
        uint8_t b62[64];
        memset(b62, 'X', 63);
        memcpy(b62, "DMS1,0,", 7);
        memcpy(b62+53, ",100\r\n", 6);
        check_invalid("len=62-badstate", b62, 62);
    }
    {
        uint8_t b63[65];
        memset(b63, 'X', 64);
        memcpy(b63, "DMS1,0,", 7);
        memcpy(b63+53, ",100\r\n", 6);
        check_invalid("len=63-badstate", b63, 63);
    }
    {
        uint8_t b64[67];
        memcpy(b64, "DMS1,0,NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN,100\r\n", 65);
        b64[64] = '\r'; b64[65] = '\n';
        check_invalid("len=66-rejected", (uint8_t*)b64, 66);
    }

    /* 13. Very short frames */
    check_invalid("len=9-short", (uint8_t*)"DMS1,0,NO", 9);
    check_invalid("len=8-short", (uint8_t*)"DMS1,0,N", 8);

    /* 14. Leading zeros in seq */
    check_valid("seq=00042", S("DMS1,00042,NORMAL,95\r\n"), DMS_STATE_NORMAL, 42, 95);

    /* 15. DMS1 at boundaries */
    check_valid("DMS1,0,UNKNOWN,0",   S("DMS1,0,UNKNOWN,0\r\n"),  DMS_STATE_UNKNOWN, 0, 0);
    check_valid("DMS1,0,NORMAL,100",  S("DMS1,0,NORMAL,100\r\n"), DMS_STATE_NORMAL, 0, 100);
    check_valid("DMS1,0,YAWN,0",      S("DMS1,0,YAWN,0\r\n"),     DMS_STATE_YAWN, 0, 0);

    /* 16. seq=1 with various confidence values in range */
    check_valid("DMS1,1,YAWN,99",     S("DMS1,1,YAWN,99\r\n"),    DMS_STATE_YAWN, 1, 99);

    printf("\n=== Results: %d tests, %d failed ===\n", tests_run, tests_failed);
    return tests_failed > 0 ? 1 : 0;
}

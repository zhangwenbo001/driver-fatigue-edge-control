#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "../../正点原子工程模板/User/dms_protocol.h"

#define DMS_RX_BUF_SIZE 64

static int tests_run = 0;
static int tests_failed = 0;

typedef struct {
    uint8_t buf[DMS_RX_BUF_SIZE];
    uint16_t idx;
    uint8_t discarding;
    int frame_count;
    dms_parsed_frame_t last_frame;
    int last_was_valid;
} rx_model_t;

static void rx_model_init(rx_model_t *m)
{
    memset(m, 0, sizeof(*m));
}

static void rx_model_feed(rx_model_t *m, uint8_t byte)
{
    if (m->discarding) {
        if (byte == '\n') {
            m->discarding = 0;
            m->idx = 0;
        }
        return;
    }

    if (m->idx >= DMS1_FRAME_MAX_BYTES) {
        m->discarding = 1;
        m->idx = 0;
        if (byte == '\n') {
            m->discarding = 0;
        }
        return;
    }

    m->buf[m->idx++] = byte;

    if (m->idx >= 2 && m->buf[m->idx-2] == '\r' && m->buf[m->idx-1] == '\n') {
        dms_parsed_frame_t frame = dms_parse(m->buf, m->idx);
        m->last_frame = frame;
        m->last_was_valid = (frame.state != DMS_STATE_INVALID);
        if (m->last_was_valid) m->frame_count++;
        m->idx = 0;
    }
}

static void rx_model_feed_bytes(rx_model_t *m, const uint8_t *data, uint16_t len)
{
    uint16_t i;
    for (i = 0; i < len; i++)
        rx_model_feed(m, data[i]);
}

#define S(str) ((const uint8_t*)(str)), ((uint16_t)strlen(str))

static void expect_frame(const char *name, int valid, dms_state_t es, uint16_t eq, uint8_t ec,
    dms_parsed_frame_t actual)
{
    tests_run++;
    int ok = 1;
    if (valid && actual.state == DMS_STATE_INVALID) {
        printf("  FAIL [%s] expected valid frame, got INVALID\n", name); ok = 0;
    }
    if (!valid && actual.state != DMS_STATE_INVALID) {
        printf("  FAIL [%s] expected INVALID, got state=%d\n", name, actual.state); ok = 0;
    }
    if (valid) {
        if (actual.state != es) { printf("  FAIL [%s] state=%d exp=%d\n", name, actual.state, es); ok=0; }
        if (actual.seq != eq)   { printf("  FAIL [%s] seq=%d exp=%d\n", name, actual.seq, eq); ok=0; }
        if (actual.confidence != ec) { printf("  FAIL [%s] conf=%d exp=%d\n", name, actual.confidence, ec); ok=0; }
    }
    if (ok) { printf("  PASS [%s]\n", name); }
    else { tests_failed++; }
}

static void expect_state(const char *name, uint8_t exp_discarding, uint16_t exp_idx,
    rx_model_t *m)
{
    tests_run++;
    int ok = 1;
    if (exp_discarding != m->discarding) {
        printf("  FAIL [%s] discarding=%d exp=%d\n", name, m->discarding, exp_discarding); ok=0;
    }
    if (exp_idx != m->idx) {
        printf("  FAIL [%s] idx=%d exp=%d\n", name, m->idx, exp_idx); ok=0;
    }
    if (ok) printf("  PASS [%s] (discarding=%d idx=%d)\n", name, m->discarding, m->idx);
    else tests_failed++;
}

int main(void)
{
    printf("=== G4-01 RX Framing Model Test (source-based simulation) ===\n\n");

    rx_model_t m;

    /* 1. Normal valid frame through framing model */
    {
        rx_model_init(&m);
        rx_model_feed_bytes(&m, S("DMS1,0,NORMAL,100\r\n"));
        expect_frame("frm-normal-valid", 1, DMS_STATE_NORMAL, 0, 100, m.last_frame);
        expect_state("frm-normal-reset", 0, 0, &m);
    }

    /* 2. Half frame: no CRLF, bytes accumulate */
    {
        rx_model_init(&m);
        rx_model_feed_bytes(&m, S("DMS1,0,NORMA"));
        expect_state("frm-half-idx", 0, 12, &m);
    }

    /* 3. Complete half frame */
    {
        rx_model_init(&m);
        rx_model_feed_bytes(&m, S("DMS1,0,NORMA"));
        rx_model_feed_bytes(&m, S("L,100\r\n"));
        expect_frame("frm-half-complete", 1, DMS_STATE_NORMAL, 0, 100, m.last_frame);
    }

    /* 4. Sticky framing: two back-to-back frames */
    {
        rx_model_init(&m);
        rx_model_feed_bytes(&m, S("DMS1,0,NORMAL,100\r\n"));
        expect_frame("frm-sticky-1st", 1, DMS_STATE_NORMAL, 0, 100, m.last_frame);
        rx_model_feed_bytes(&m, S("DMS1,1,YAWN,50\r\n"));
        expect_frame("frm-sticky-2nd", 1, DMS_STATE_YAWN, 1, 50, m.last_frame);
    }

    /* 5. idx reaches 62 (62 bytes stored, CRLF found → frame parsed, idx=0) */
    {
        /* Build: DMS1, + 46*A + NORMAL\r\n = 5 + 46 + 8 = 59 bytes, too short.
           Build: DMS1,00042, + 44*A + NORMAL\r\n = 11 + 44 + 8 = 63. 
           But with seq=00042, dms_parse should parse OK.
           Let's do: DMS1,65535, + 44*A + FATIGUE\r\n = 11+44+9 = 64, no.
           Let's do: DMS1,00000, + 44*A + NORMAL\r\n = 11+44+8 = 63.
           Wait: DMS1,0, + 48*A + NORMAL\r\n = 7+48+8 = 63.
           For 62: DMS1,0, + 47*A + NORMAL\r\n = 7+47+8 = 62.
           But the state field will be "AAA...NORMAL" which is invalid.
           We need valid parse at 62/63. Let me think differently.
           
           Use YAWN (4 bytes) and pad differently:
           For 62: DMS1,0, + 51*A + YAWN\r\n = 7+51+6 = 64. No.
           For 62: DMS1,0,YAWN\r\n = 13 bytes. Pad before ending with more digits.
           
           Actually, let me just focus on the framing MODEL, not dms_parse result.
           The key test is: at exactly 62 bytes with CRLF ending, framing model 
           completes frame (idx=0). At 63 bytes same. At 64th byte, discard triggers.
           We test dms_parse acceptance separately in the protocol test.
        */
        uint8_t f62[64];
        memset(f62, 0, sizeof(f62));
        memcpy(f62, "DMS1,0,NORMAL,100\r\n", 19);
        f62[17] = '\r'; f62[18] = '\n';
        /* pad to 62 bytes with junk after a valid short frame? No, that won't work.
           Let me instead construct a frame where the state field is long but valid.
           Actually, adjust: use many bytes of valid prefix + state padding. 
           
           Simplest: create a 62-byte buffer ending in \r\n, feed it byte by byte,
           and verify idx goes to 0 after the CRLF triggers frame completion.
           We don't need dms_parse to accept it — we're testing the framing machine.
        */
        uint8_t buf62[64];
        memset(buf62, 'X', 60);
        buf62[60] = '\r'; buf62[61] = '\n';
        rx_model_init(&m);
        rx_model_feed_bytes(&m, buf62, 62);
        expect_state("frm-62byte-crlf", 0, 0, &m);
    }

    /* 6. 63 bytes with CRLF ending */
    {
        uint8_t buf63[65];
        memset(buf63, 'X', 61);
        buf63[61] = '\r'; buf63[62] = '\n';
        rx_model_init(&m);
        rx_model_feed_bytes(&m, buf63, 63);
        expect_state("frm-63byte-crlf", 0, 0, &m);
    }

    /* 7. 64th byte non-LF triggers discard */
    {
        uint8_t buf[66];
        memset(buf, 'X', 63);
        buf[62] = 'X'; /* byte 63 stored */
        buf[63] = 'Y'; /* byte 64: non-LF, triggers discard */
        rx_model_init(&m);
        rx_model_feed_bytes(&m, buf, 64);
        expect_state("frm-64-nonlf-disc", 1, 0, &m);
    }

    /* 8. 64th byte IS LF: immediate exit from discard (G4 fix) */
    {
        uint8_t buf[66];
        memset(buf, 'X', 63);
        buf[62] = '\n'; /* byte 63 stored as... wait.
           Hmm let me trace: 
           Byte 1..63: idx goes 0..62, each time idx < 63 so stored.
           After storing byte 63, idx = 63.
           Next byte (byte 64): idx(63) >= DMS1_FRAME_MAX_BYTES(63) → discard=1, idx=0.
           If byte 64 IS '\n': discard=1, '\n' matches → discard=0, idx=0.
           So "64th byte IS LF" means the 64th byte in the stream.
        */
        memset(buf, 'A', 63); /* fill first 63 bytes with non-LF */
        buf[63] = '\n';       /* 64th byte = LF */
        rx_model_init(&m);
        rx_model_feed_bytes(&m, buf, 64);
        expect_state("frm-64th-is-LF", 0, 0, &m);
    }

    /* 9. After discard+LF resync, next legal frame accepted */
    {
        rx_model_init(&m);
        /* 63 As + LF as 64th → discard exit */
        uint8_t gar[65];
        memset(gar, 'A', 63);
        gar[63] = '\n';
        rx_model_feed_bytes(&m, gar, 64);
        /* now feed a valid frame */
        rx_model_feed_bytes(&m, S("DMS1,42,YAWN,88\r\n"));
        expect_frame("frm-resync-next-ok", 1, DMS_STATE_YAWN, 42, 88, m.last_frame);
    }

    /* 10. Invalid frame (bad header) through framing: idx resets but no valid frame */
    {
        rx_model_init(&m);
        rx_model_feed_bytes(&m, S("XMS1,0,NORMAL,100\r\n"));
        expect_state("frm-bad-header-idx", 0, 0, &m);
    }

    /* 11. Consecutive valid frames: frame_count increments properly */
    {
        rx_model_init(&m);
        rx_model_feed_bytes(&m, S("DMS1,0,NORMAL,100\r\n"));
        rx_model_feed_bytes(&m, S("DMS1,1,NORMAL,100\r\n"));
        rx_model_feed_bytes(&m, S("DMS1,2,NORMAL,100\r\n"));
        tests_run++;
        if (m.frame_count != 3) {
            printf("  FAIL [frm-count-3] count=%d exp=3\n", m.frame_count); tests_failed++;
        } else {
            printf("  PASS [frm-count-3]\n");
        }
    }

    printf("\n=== Results: %d tests, %d failed ===\n", tests_run, tests_failed);
    return tests_failed > 0 ? 1 : 0;
}

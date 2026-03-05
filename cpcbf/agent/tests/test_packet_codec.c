/*
 * CPCBF — Packet Codec Unit Tests
 * Build: gcc -o test_packet_codec test_packet_codec.c ../common/benchmark_packet.c -I../common
 */
#include "benchmark_packet.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static int tests_run = 0;
static int tests_passed = 0;

#define TEST(name) do { \
    tests_run++; \
    printf("  [%02d] %-40s ", tests_run, name); \
    fflush(stdout); \
} while(0)

#define PASS() do { tests_passed++; printf("PASS\n"); } while(0)
#define FAIL(msg) do { printf("FAIL: %s\n", msg); } while(0)

/* 1. Roundtrip: encode then decode, fields match */
static void test_roundtrip(void)
{
    TEST("roundtrip encode/decode");

    bench_packet_t tx = {0};
    tx.msg_type = MSG_PING;
    tx.seq_num = 42;
    tx.timestamp = 123456;
    tx.payload_len = 5;
    memcpy(tx.payload, "Hello", 5);

    uint8_t buf[256];
    int enc = bench_packet_encode(&tx, buf, sizeof(buf));
    if (enc != 5 + BENCH_OVERHEAD) { FAIL("wrong encoded length"); return; }

    bench_packet_t rx = {0};
    int rc = bench_packet_decode(buf, (size_t)enc, &rx);
    if (rc != 0) { FAIL("decode failed"); return; }

    if (rx.magic != BENCH_MAGIC)       { FAIL("magic mismatch"); return; }
    if (rx.msg_type != MSG_PING)       { FAIL("msg_type mismatch"); return; }
    if (rx.seq_num != 42)              { FAIL("seq_num mismatch"); return; }
    if (rx.timestamp != 123456)        { FAIL("timestamp mismatch"); return; }
    if (rx.payload_len != 5)           { FAIL("payload_len mismatch"); return; }
    if (memcmp(rx.payload, "Hello", 5)) { FAIL("payload mismatch"); return; }

    PASS();
}

/* 2. Known CRC vectors */
static void test_crc_known_vectors(void)
{
    TEST("CRC-32 known vectors");

    /* CRC-32 of "" is 0x00000000 */
    uint32_t crc0 = bench_crc32((const uint8_t *)"", 0);
    if (crc0 != 0x00000000) { FAIL("empty CRC wrong"); return; }

    /* CRC-32 of "123456789" is 0xCBF43926 */
    uint32_t crc9 = bench_crc32((const uint8_t *)"123456789", 9);
    if (crc9 != 0xCBF43926) { FAIL("123456789 CRC wrong"); return; }

    PASS();
}

/* 3. Corrupt magic byte */
static void test_corrupt_magic(void)
{
    TEST("reject corrupt magic byte");

    bench_packet_t tx = {0};
    tx.msg_type = MSG_PONG;
    tx.payload_len = 0;

    uint8_t buf[64];
    int enc = bench_packet_encode(&tx, buf, sizeof(buf));
    if (enc < 0) { FAIL("encode failed"); return; }

    buf[0] = 0x00; /* corrupt magic */

    bench_packet_t rx = {0};
    int rc = bench_packet_decode(buf, (size_t)enc, &rx);
    if (rc != -1) { FAIL("should reject corrupt magic"); return; }

    PASS();
}

/* 4. Corrupt CRC */
static void test_corrupt_crc(void)
{
    TEST("detect corrupt CRC");

    bench_packet_t tx = {0};
    tx.msg_type = MSG_FLOOD;
    tx.payload_len = 4;
    memcpy(tx.payload, "test", 4);

    uint8_t buf[64];
    int enc = bench_packet_encode(&tx, buf, sizeof(buf));
    if (enc < 0) { FAIL("encode failed"); return; }

    buf[enc - 1] ^= 0xFF; /* flip last byte of CRC */

    bench_packet_t rx = {0};
    int rc = bench_packet_decode(buf, (size_t)enc, &rx);
    if (rc != -2) { FAIL("should return -2 for CRC error"); return; }

    PASS();
}

/* 5. Truncated buffer */
static void test_truncated(void)
{
    TEST("reject truncated buffer");

    bench_packet_t tx = {0};
    tx.msg_type = MSG_PING;
    tx.payload_len = 10;
    memset(tx.payload, 'A', 10);

    uint8_t buf[64];
    int enc = bench_packet_encode(&tx, buf, sizeof(buf));
    if (enc < 0) { FAIL("encode failed"); return; }

    bench_packet_t rx = {0};
    /* Pass only half the buffer */
    int rc = bench_packet_decode(buf, (size_t)(enc / 2), &rx);
    if (rc != -1) { FAIL("should reject truncated buffer"); return; }

    PASS();
}

/* 6. Maximum payload */
static void test_max_payload(void)
{
    TEST("max payload roundtrip");

    bench_packet_t tx = {0};
    tx.msg_type = MSG_FLOOD;
    tx.seq_num = 0xFFFF;
    tx.timestamp = 0xDEADBEEF;
    tx.payload_len = BENCH_MAX_PAYLOAD;
    memset(tx.payload, 0xAB, BENCH_MAX_PAYLOAD);

    size_t needed = BENCH_MAX_PAYLOAD + BENCH_OVERHEAD;
    uint8_t *buf = malloc(needed);
    if (!buf) { FAIL("malloc failed"); return; }

    int enc = bench_packet_encode(&tx, buf, needed);
    if (enc != (int)needed) { FAIL("wrong encoded length"); free(buf); return; }

    bench_packet_t rx = {0};
    int rc = bench_packet_decode(buf, (size_t)enc, &rx);
    if (rc != 0) { FAIL("decode failed"); free(buf); return; }
    if (rx.payload_len != BENCH_MAX_PAYLOAD) { FAIL("payload_len wrong"); free(buf); return; }

    int cmp = 1;
    for (uint16_t i = 0; i < BENCH_MAX_PAYLOAD; i++) {
        if (rx.payload[i] != 0xAB) { cmp = 0; break; }
    }
    if (!cmp) { FAIL("payload data mismatch"); free(buf); return; }

    free(buf);
    PASS();
}

/* 7. Zero payload */
static void test_zero_payload(void)
{
    TEST("zero payload roundtrip");

    bench_packet_t tx = {0};
    tx.msg_type = MSG_CTRL;
    tx.seq_num = 1;
    tx.payload_len = 0;

    uint8_t buf[64];
    int enc = bench_packet_encode(&tx, buf, sizeof(buf));
    if (enc != BENCH_OVERHEAD) { FAIL("wrong encoded length for zero payload"); return; }

    bench_packet_t rx = {0};
    int rc = bench_packet_decode(buf, (size_t)enc, &rx);
    if (rc != 0) { FAIL("decode failed"); return; }
    if (rx.payload_len != 0) { FAIL("payload_len should be 0"); return; }

    PASS();
}

/* 8. All message types */
static void test_all_msg_types(void)
{
    TEST("all message types roundtrip");

    uint8_t types[] = { MSG_PING, MSG_PONG, MSG_FLOOD, MSG_FLOOD_ACK, MSG_CTRL };
    uint8_t buf[64];

    for (int t = 0; t < 5; t++) {
        bench_packet_t tx = {0};
        tx.msg_type = types[t];
        tx.payload_len = 1;
        tx.payload[0] = types[t];

        int enc = bench_packet_encode(&tx, buf, sizeof(buf));
        if (enc < 0) { FAIL("encode failed"); return; }

        bench_packet_t rx = {0};
        int rc = bench_packet_decode(buf, (size_t)enc, &rx);
        if (rc != 0) { FAIL("decode failed"); return; }
        if (rx.msg_type != types[t]) { FAIL("msg_type mismatch"); return; }
    }

    PASS();
}

/* 9. Network byte order verification */
static void test_byte_order(void)
{
    TEST("network byte order (big-endian)");

    bench_packet_t tx = {0};
    tx.msg_type = MSG_PING;
    tx.seq_num = 0x0102;
    tx.timestamp = 0x01020304;
    tx.payload_len = 0;

    uint8_t buf[64];
    int enc = bench_packet_encode(&tx, buf, sizeof(buf));
    if (enc < 0) { FAIL("encode failed"); return; }

    /* seq_num at offset 2-3 should be big-endian: 0x01, 0x02 */
    if (buf[2] != 0x01 || buf[3] != 0x02) {
        FAIL("seq_num not big-endian"); return;
    }
    /* timestamp at offset 4-7 should be 0x01, 0x02, 0x03, 0x04 */
    if (buf[4] != 0x01 || buf[5] != 0x02 || buf[6] != 0x03 || buf[7] != 0x04) {
        FAIL("timestamp not big-endian"); return;
    }

    PASS();
}

/* 10. Sequence number wrap-around */
static void test_seq_wrap(void)
{
    TEST("sequence number wrap (0xFFFF)");

    bench_packet_t tx = {0};
    tx.msg_type = MSG_PING;
    tx.seq_num = 0xFFFF;
    tx.payload_len = 2;
    tx.payload[0] = 0xFF;
    tx.payload[1] = 0xFE;

    uint8_t buf[64];
    int enc = bench_packet_encode(&tx, buf, sizeof(buf));
    if (enc < 0) { FAIL("encode failed"); return; }

    bench_packet_t rx = {0};
    int rc = bench_packet_decode(buf, (size_t)enc, &rx);
    if (rc != 0) { FAIL("decode failed"); return; }
    if (rx.seq_num != 0xFFFF) { FAIL("seq_num wrong after wrap"); return; }

    PASS();
}

int main(void)
{
    printf("CPCBF Packet Codec Tests\n");
    printf("========================\n");

    test_roundtrip();
    test_crc_known_vectors();
    test_corrupt_magic();
    test_corrupt_crc();
    test_truncated();
    test_max_payload();
    test_zero_payload();
    test_all_msg_types();
    test_byte_order();
    test_seq_wrap();

    printf("========================\n");
    printf("Results: %d/%d passed\n", tests_passed, tests_run);

    return (tests_passed == tests_run) ? 0 : 1;
}

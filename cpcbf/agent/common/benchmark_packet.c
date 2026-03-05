/*
 * CPCBF — Benchmark Packet Codec + CRC-32
 */
#include "benchmark_packet.h"
#include <string.h>

/* ---- CRC-32 (IEEE 802.3, polynomial 0xEDB88320) ---- */

static uint32_t crc_table[256];
static int crc_table_ready = 0;

static void crc_table_init(void)
{
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int j = 0; j < 8; j++) {
            if (c & 1)
                c = 0xEDB88320u ^ (c >> 1);
            else
                c >>= 1;
        }
        crc_table[i] = c;
    }
    crc_table_ready = 1;
}

uint32_t bench_crc32(const uint8_t *data, size_t len)
{
    if (!crc_table_ready)
        crc_table_init();

    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++)
        crc = crc_table[(crc ^ data[i]) & 0xFF] ^ (crc >> 8);
    return crc ^ 0xFFFFFFFFu;
}

/* ---- Helpers for network byte order (big-endian) ---- */

static void put_u16(uint8_t *buf, uint16_t val)
{
    buf[0] = (uint8_t)(val >> 8);
    buf[1] = (uint8_t)(val);
}

static void put_u32(uint8_t *buf, uint32_t val)
{
    buf[0] = (uint8_t)(val >> 24);
    buf[1] = (uint8_t)(val >> 16);
    buf[2] = (uint8_t)(val >> 8);
    buf[3] = (uint8_t)(val);
}

static uint16_t get_u16(const uint8_t *buf)
{
    return (uint16_t)((buf[0] << 8) | buf[1]);
}

static uint32_t get_u32(const uint8_t *buf)
{
    return ((uint32_t)buf[0] << 24) |
           ((uint32_t)buf[1] << 16) |
           ((uint32_t)buf[2] << 8)  |
           ((uint32_t)buf[3]);
}

/* ---- Encode ---- */

int bench_packet_encode(const bench_packet_t *pkt, uint8_t *buf, size_t buf_len)
{
    if (!pkt || !buf)
        return -1;
    if (pkt->payload_len > BENCH_MAX_PAYLOAD)
        return -1;

    size_t total = (size_t)pkt->payload_len + BENCH_OVERHEAD;
    if (buf_len < total)
        return -1;

    /* Header */
    buf[0] = BENCH_MAGIC;
    buf[1] = pkt->msg_type;
    put_u16(&buf[2], pkt->seq_num);
    put_u32(&buf[4], pkt->timestamp);
    put_u16(&buf[8], pkt->payload_len);

    /* Payload */
    if (pkt->payload_len > 0)
        memcpy(&buf[BENCH_HEADER_LEN], pkt->payload, pkt->payload_len);

    /* CRC over header + payload */
    uint32_t crc = bench_crc32(buf, BENCH_HEADER_LEN + pkt->payload_len);
    put_u32(&buf[BENCH_HEADER_LEN + pkt->payload_len], crc);

    return (int)total;
}

/* ---- Decode ---- */

int bench_packet_decode(const uint8_t *buf, size_t buf_len, bench_packet_t *pkt)
{
    if (!buf || !pkt)
        return -1;

    /* Minimum size: header + CRC, no payload */
    if (buf_len < BENCH_OVERHEAD)
        return -1;

    /* Check magic */
    if (buf[0] != BENCH_MAGIC)
        return -1;

    uint16_t payload_len = get_u16(&buf[8]);
    if (payload_len > BENCH_MAX_PAYLOAD)
        return -1;

    size_t expected = (size_t)payload_len + BENCH_OVERHEAD;
    if (buf_len < expected)
        return -1;

    /* Verify CRC */
    uint32_t wire_crc = get_u32(&buf[BENCH_HEADER_LEN + payload_len]);
    uint32_t calc_crc = bench_crc32(buf, BENCH_HEADER_LEN + payload_len);
    if (wire_crc != calc_crc)
        return -2;

    /* Populate struct */
    pkt->magic       = buf[0];
    pkt->msg_type    = buf[1];
    pkt->seq_num     = get_u16(&buf[2]);
    pkt->timestamp   = get_u32(&buf[4]);
    pkt->payload_len = payload_len;
    if (payload_len > 0)
        memcpy(pkt->payload, &buf[BENCH_HEADER_LEN], payload_len);
    pkt->crc = wire_crc;

    return 0;
}

/*
 * CPCBF — Cross-Platform Communication Benchmarking Framework
 * Benchmark Packet Format
 *
 * Wire format (network byte order):
 *   [Magic 1B][MsgType 1B][SeqNum 2B][Timestamp 4B][PayloadLen 2B][Payload NB][CRC32 4B]
 *   Total overhead: 14 bytes (10 header + 4 trailer)
 */
#ifndef CPCBF_BENCHMARK_PACKET_H
#define CPCBF_BENCHMARK_PACKET_H

#include <stdint.h>
#include <stddef.h>

#define BENCH_MAGIC      0xBE
#define BENCH_HEADER_LEN 10
#define BENCH_CRC_LEN    4
#define BENCH_OVERHEAD    (BENCH_HEADER_LEN + BENCH_CRC_LEN) /* 14 */
#ifndef BENCH_MAX_PAYLOAD
#define BENCH_MAX_PAYLOAD 8192
#endif

/* Message types */
#define MSG_PING      0x01
#define MSG_PONG      0x02
#define MSG_FLOOD     0x03
#define MSG_FLOOD_ACK 0x04
#define MSG_CTRL      0x05

/* Decoded packet structure */
typedef struct {
    uint8_t  magic;
    uint8_t  msg_type;
    uint16_t seq_num;
    uint32_t timestamp;
    uint16_t payload_len;
    uint8_t  payload[BENCH_MAX_PAYLOAD];
    uint32_t crc;
} bench_packet_t;

/*
 * Encode a packet into a wire buffer.
 * Returns total bytes written, or -1 on error.
 * buf must be at least payload_len + BENCH_OVERHEAD bytes.
 */
int bench_packet_encode(const bench_packet_t *pkt, uint8_t *buf, size_t buf_len);

/*
 * Decode a wire buffer into a packet struct.
 * Returns 0 on success, -1 on format error, -2 on CRC mismatch.
 */
int bench_packet_decode(const uint8_t *buf, size_t buf_len, bench_packet_t *pkt);

/*
 * Compute CRC-32 (IEEE 802.3 polynomial) over data.
 */
uint32_t bench_crc32(const uint8_t *data, size_t len);

#endif /* CPCBF_BENCHMARK_PACKET_H */

/*
 * CPCBF — Sync Barrier
 * Protocol-agnostic peer synchronization using 5-byte beacons.
 *
 * Beacon format: [0xC0, type, phase, idx_hi, idx_lo]
 *   type  0x01 = SYNC_REQ  ("I'm ready for test idx, are you?")
 *   type  0x02 = SYNC_ACK  ("I see you, same test idx, let's go")
 *   phase 0x00 = pre-test, 0x01 = post-test
 *
 * Match requires both phase AND test_idx to agree.
 * If the peer's idx > ours (same phase): return SYNC_IDX_MISMATCH
 * If the peer's idx < ours (same phase): ignore (peer will catch up)
 * If phases differ: ignore (peer is at a different pipeline stage)
 */
#include "sync_barrier.h"
#include "platform_hal.h"

#include <string.h>

#define SYNC_MAGIC    0xC0
#define SYNC_TYPE_REQ 0x01
#define SYNC_TYPE_ACK 0x02
#define BEACON_LEN    5
#define INTERVAL_MS   200

static void build_beacon(uint8_t *buf, uint8_t type,
                          uint8_t phase, uint16_t idx)
{
    buf[0] = SYNC_MAGIC;
    buf[1] = type;
    buf[2] = phase;
    buf[3] = (uint8_t)(idx >> 8);
    buf[4] = (uint8_t)(idx & 0xFF);
}

static uint16_t beacon_idx(const uint8_t *buf)
{
    return ((uint16_t)buf[3] << 8) | buf[4];
}

int sync_barrier_wait(protocol_adapter_t *adapter, uint32_t timeout_ms,
                      uint16_t my_test_idx, uint8_t my_phase,
                      uint16_t *peer_test_idx_out)
{
    uint32_t elapsed = 0;
    int got_ack = 0;
    uint8_t req[BEACON_LEN], ack[BEACON_LEN];

    build_beacon(req, SYNC_TYPE_REQ, my_phase, my_test_idx);
    build_beacon(ack, SYNC_TYPE_ACK, my_phase, my_test_idx);

    platform_log("sync: waiting for peer (timeout %u ms, phase %u, test_idx %u)",
                 timeout_ms, my_phase, my_test_idx);

    while (elapsed < timeout_ms) {
        /* Send SYNC_REQ with our phase + test_idx */
        adapter->send(adapter, req, BEACON_LEN);

        /* Short recv */
        uint8_t buf[8];
        size_t out_len = 0;
        int rc = adapter->recv(adapter, buf, sizeof(buf), &out_len, INTERVAL_MS);

        if (rc == ADAPTER_OK && out_len >= BEACON_LEN && buf[0] == SYNC_MAGIC) {
            uint8_t  peer_phase = buf[2];
            uint16_t peer_idx   = beacon_idx(buf);

            /* Only consider beacons with matching phase */
            if (peer_phase == my_phase) {
                if (peer_idx == my_test_idx) {
                    /* Same test, same phase — proceed with normal sync */
                    if (buf[1] == SYNC_TYPE_ACK) {
                        got_ack = 1;
                        break;
                    }
                    if (buf[1] == SYNC_TYPE_REQ) {
                        adapter->send(adapter, ack, BEACON_LEN);
                        got_ack = 1;
                        break;
                    }
                } else if (peer_idx > my_test_idx) {
                    /* Peer is ahead — we need to skip forward */
                    platform_log("sync: idx mismatch (mine=%u peer=%u) — skipping forward",
                                 my_test_idx, peer_idx);
                    if (peer_test_idx_out)
                        *peer_test_idx_out = peer_idx;
                    return SYNC_IDX_MISMATCH;
                }
                /* else peer_idx < my_test_idx: peer is behind, ignore */
            }
            /* else different phase: ignore */
        }

        elapsed += INTERVAL_MS;
    }

    if (!got_ack) {
        platform_log("sync: timeout after %u ms", timeout_ms);
        return SYNC_TIMEOUT;
    }

    /* Send 3 trailing ACKs so the peer also converges */
    for (int i = 0; i < 3; i++) {
        adapter->send(adapter, ack, BEACON_LEN);
        platform_sleep_ms(50);
    }

    /* Drain any remaining sync messages from the socket */
    for (int i = 0; i < 20; i++) {
        uint8_t drain[8];
        size_t drain_len = 0;
        int drc = adapter->recv(adapter, drain, sizeof(drain), &drain_len, 10);
        if (drc != ADAPTER_OK || drain_len == 0)
            break;
    }

    platform_log("sync: complete (phase %u, test_idx %u)", my_phase, my_test_idx);
    return SYNC_OK;
}

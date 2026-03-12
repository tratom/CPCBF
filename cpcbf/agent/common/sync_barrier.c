/*
 * CPCBF — Sync Barrier
 * Protocol-agnostic peer synchronization using 2-byte beacons.
 *
 * SYNC_REQ [0xC0, 0x01] — "I'm ready, are you?"
 * SYNC_ACK [0xC0, 0x02] — "I see you, let's go"
 *
 * Both sides loop: send REQ, short recv.
 *   - On receiving REQ → reply ACK, done.
 *   - On receiving ACK → done.
 * After convergence: 3 trailing ACKs + drain.
 */
#include "sync_barrier.h"
#include "platform_hal.h"

#include <string.h>

/* Platform-agnostic millisecond sleep */
#if defined(_WIN32)
  #include <windows.h>
  #define SLEEP_MS(ms) Sleep(ms)
#else
  #include <unistd.h>
  #define SLEEP_MS(ms) usleep((ms) * 1000u)
#endif

static const uint8_t SYNC_REQ[2] = {0xC0, 0x01};
static const uint8_t SYNC_ACK[2] = {0xC0, 0x02};
#define INTERVAL_MS 200

int sync_barrier_wait(protocol_adapter_t *adapter, uint32_t timeout_ms)
{
    uint32_t elapsed = 0;
    int got_ack = 0;

    platform_log("sync: waiting for peer (timeout %u ms)", timeout_ms);

    while (elapsed < timeout_ms) {
        /* Send SYNC_REQ */
        adapter->send(adapter, SYNC_REQ, 2);

        /* Short recv with INTERVAL_MS timeout */
        uint8_t buf[8];
        size_t out_len = 0;
        int rc = adapter->recv(adapter, buf, sizeof(buf), &out_len, INTERVAL_MS);

        if (rc == ADAPTER_OK && out_len >= 2 && buf[0] == 0xC0) {
            if (buf[1] == 0x02) {
                /* Got SYNC_ACK — peer already saw our REQ */
                got_ack = 1;
                break;
            }
            if (buf[1] == 0x01) {
                /* Got SYNC_REQ — peer is ready, reply with ACK */
                adapter->send(adapter, SYNC_ACK, 2);
                got_ack = 1;
                break;
            }
        }

        elapsed += INTERVAL_MS;
    }

    if (!got_ack) {
        platform_log("sync: timeout after %u ms", timeout_ms);
        return -1;
    }

    /* Send 3 trailing ACKs so the peer also converges */
    for (int i = 0; i < 3; i++) {
        adapter->send(adapter, SYNC_ACK, 2);
        SLEEP_MS(50);
    }

    /* Drain any remaining sync messages from the socket */
    for (int i = 0; i < 20; i++) {
        uint8_t drain[8];
        size_t drain_len = 0;
        int rc = adapter->recv(adapter, drain, sizeof(drain), &drain_len, 10);
        if (rc != ADAPTER_OK || drain_len == 0)
            break;
    }

    platform_log("sync complete");
    return 0;
}

/*
 * CPCBF — Sync Barrier
 * Protocol-agnostic peer synchronization over any adapter.
 */
#ifndef CPCBF_SYNC_BARRIER_H
#define CPCBF_SYNC_BARRIER_H

#include "protocol_adapter.h"
#include <stdint.h>

/*
 * Block until the remote peer is also ready, or timeout.
 * Uses 2-byte beacon messages over the adapter's send/recv.
 *
 * Returns 0 on success, -1 on timeout.
 */
int sync_barrier_wait(protocol_adapter_t *adapter, uint32_t timeout_ms);

#endif /* CPCBF_SYNC_BARRIER_H */

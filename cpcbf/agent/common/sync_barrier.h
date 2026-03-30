/*
 * CPCBF — Sync Barrier
 * Protocol-agnostic peer synchronization over any adapter.
 */
#ifndef CPCBF_SYNC_BARRIER_H
#define CPCBF_SYNC_BARRIER_H

#include "protocol_adapter.h"
#include <stdint.h>

/* Sync return codes */
#define SYNC_OK            0
#define SYNC_TIMEOUT      -1
#define SYNC_IDX_MISMATCH -2

/* Sync phases (pre-test vs post-test) */
#define SYNC_PHASE_PRE  0
#define SYNC_PHASE_POST 1

/*
 * Block until the remote peer is also ready for the same test, or timeout.
 * Uses 5-byte beacon messages: [0xC0, type, phase, idx_hi, idx_lo].
 *
 * my_test_idx:       this side's current test index
 * my_phase:          SYNC_PHASE_PRE (0) or SYNC_PHASE_POST (1)
 * peer_test_idx_out: if non-NULL and return is SYNC_IDX_MISMATCH,
 *                    set to the peer's test_idx
 *
 * Returns SYNC_OK (0) on success,
 *         SYNC_TIMEOUT (-1) on timeout,
 *         SYNC_IDX_MISMATCH (-2) if peer has a higher test_idx (same phase).
 */
int sync_barrier_wait(protocol_adapter_t *adapter, uint32_t timeout_ms,
                      uint16_t my_test_idx, uint8_t my_phase,
                      uint16_t *peer_test_idx_out);

#endif /* CPCBF_SYNC_BARRIER_H */

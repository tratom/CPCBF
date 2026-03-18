/*
 * CPCBF — Test Engine
 * Orchestrates ping-pong and flood benchmark modes.
 */
#ifndef CPCBF_TEST_ENGINE_H
#define CPCBF_TEST_ENGINE_H

#include <stdint.h>
#include <stddef.h>
#include "protocol_adapter.h"

/* Test modes */
typedef enum {
    TEST_MODE_PING_PONG = 0,
    TEST_MODE_FLOOD     = 1,
    TEST_MODE_RSSI      = 2,
} test_mode_t;

/* Test configuration */
typedef struct {
    test_mode_t mode;
    adapter_role_t role;
    uint16_t payload_size;    /* bytes of payload per packet */
    uint32_t repetitions;     /* number of measured packets */
    uint32_t warmup;          /* warmup packets (excluded from stats) */
    uint32_t timeout_ms;      /* per-packet timeout */
    uint32_t inter_packet_us; /* delay between sends (flood mode) */
    uint8_t  aggregate_only;  /* 1 = skip per-packet storage (flood on constrained devices) */
} test_config_t;

/* Per-packet result */
typedef struct {
    uint16_t seq;
    uint32_t tx_us;
    uint32_t rx_us;
    uint32_t rtt_us;
    int      rssi;
    uint8_t  crc_ok;
    uint8_t  lost;
    uint8_t  is_warmup;
} packet_result_t;

/* Aggregate results */
typedef struct {
    uint32_t packets_sent;
    uint32_t packets_received;
    uint32_t packets_lost;
    uint32_t crc_errors;
    uint32_t warmup_count;
    uint32_t result_count;
    uint32_t start_us;        /* timestamp of first packet */
    uint32_t end_us;          /* timestamp of last packet */
    uint8_t  aggregate_only;  /* 1 = no per-packet results stored */
#if defined(__cplusplus)
    packet_result_t results[1]; /* C++ compat — allocated via calloc with extra space */
#else
    packet_result_t results[]; /* C99 flexible array */
#endif
} test_results_t;

/*
 * Allocate a test_results_t with space for max_packets entries.
 * Returns NULL on allocation failure.
 */
test_results_t *test_results_alloc(uint32_t max_packets);

/* Free a test_results_t. */
void test_results_free(test_results_t *r);

/*
 * Run a benchmark test.
 * The adapter must be initialized before calling this.
 * Returns allocated test_results_t on success, NULL on error.
 */
test_results_t *test_engine_run(protocol_adapter_t *adapter,
                                const test_config_t *cfg);

/*
 * Serialize results to JSON.
 * Returns a malloc'd string. Caller must free().
 */
char *test_results_to_json(const test_results_t *results,
                           const test_config_t *cfg);

#endif /* CPCBF_TEST_ENGINE_H */

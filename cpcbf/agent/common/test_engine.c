/*
 * CPCBF — Test Engine Implementation
 */
#include "test_engine.h"
#include "benchmark_packet.h"
#include "platform_hal.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

test_results_t *test_results_alloc(uint32_t max_packets)
{
    size_t sz = sizeof(test_results_t) + max_packets * sizeof(packet_result_t);
    test_results_t *r = calloc(1, sz);
    return r;
}

void test_results_free(test_results_t *r)
{
    free(r);
}

/* ---- Ping-pong sender ---- */

static void run_ping_pong_sender(protocol_adapter_t *adapter,
                                 const test_config_t *cfg,
                                 test_results_t *res)
{
    uint32_t total = cfg->warmup + cfg->repetitions;
    uint8_t tx_buf[BENCH_MAX_PAYLOAD + BENCH_OVERHEAD];
    uint8_t rx_buf[BENCH_MAX_PAYLOAD + BENCH_OVERHEAD];
    bench_packet_t pkt;
    uint32_t consecutive_timeouts = 0;

    memset(&pkt, 0, sizeof(pkt));
    pkt.msg_type = MSG_PING;

    /* Fill payload with a repeating pattern */
    for (uint16_t i = 0; i < cfg->payload_size; i++)
        pkt.payload[i] = (uint8_t)(i & 0xFF);
    pkt.payload_len = cfg->payload_size;

    for (uint32_t i = 0; i < total; i++) {
        pkt.seq_num = (uint16_t)(i & 0xFFFF);
        pkt.timestamp = platform_timestamp_us();

        int enc_len = bench_packet_encode(&pkt, tx_buf, sizeof(tx_buf));
        if (enc_len < 0) {
            platform_log("encode error seq=%u", i);
            continue;
        }

        uint32_t tx_us = platform_timestamp_us();
        int rc = adapter->send(adapter, tx_buf, (size_t)enc_len);
        res->packets_sent++;

        packet_result_t *pr = &res->results[res->result_count];
        pr->seq = pkt.seq_num;
        pr->tx_us = tx_us;
        pr->is_warmup = (i < cfg->warmup) ? 1 : 0;

        if (rc != ADAPTER_OK) {
            pr->lost = 1;
            res->packets_lost++;
            res->result_count++;
            consecutive_timeouts++;
            if (consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS) {
                platform_log("pp_tx: %u consecutive send failures, aborting",
                             consecutive_timeouts);
                res->early_aborted = 1;
                break;
            }
            continue;
        }

        /* Wait for PONG */
        size_t rx_len = 0;
        rc = adapter->recv(adapter, rx_buf, sizeof(rx_buf), &rx_len, cfg->timeout_ms);
        uint32_t rx_us = platform_timestamp_us();

        if (rc == ADAPTER_ERR_TIMEOUT || rc == ADAPTER_ERR_RECV) {
            pr->lost = 1;
            res->packets_lost++;
            res->result_count++;
            consecutive_timeouts++;
            if (consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS) {
                platform_log("pp_tx: %u consecutive timeouts, aborting",
                             consecutive_timeouts);
                res->early_aborted = 1;
                break;
            }
            continue;
        }

        bench_packet_t rpkt;
        int dec_rc = bench_packet_decode(rx_buf, rx_len, &rpkt);
        if (dec_rc == -2) {
            pr->crc_ok = 0;
            res->crc_errors++;
        } else if (dec_rc == 0) {
            pr->crc_ok = 1;
        } else {
            pr->lost = 1;
            res->packets_lost++;
            res->result_count++;
            continue;
        }

        /* Verify seq matches — discard stale PONGs from previous iterations */
        if (rpkt.seq_num != pkt.seq_num) {
            platform_log("pp_tx: seq mismatch, expected %u got %u — draining",
                         pkt.seq_num, rpkt.seq_num);
            /* Drain any additional stale packets from the buffer */
            size_t drain_len = 0;
            while (adapter->recv(adapter, rx_buf, sizeof(rx_buf),
                                 &drain_len, 0) == ADAPTER_OK) {
                drain_len = 0;
            }
            pr->lost = 1;
            res->packets_lost++;
            res->result_count++;
            continue;
        }

        pr->rx_us = rx_us;
        pr->rtt_us = rx_us - tx_us;
        res->packets_received++;
        consecutive_timeouts = 0;

        res->result_count++;

        if (cfg->inter_packet_us > 0)
            platform_sleep_us(cfg->inter_packet_us);
    }
}

/* ---- Ping-pong receiver ---- */

static void run_ping_pong_receiver(protocol_adapter_t *adapter,
                                   const test_config_t *cfg,
                                   test_results_t *res)
{
    uint32_t total = cfg->warmup + cfg->repetitions;
    uint8_t rx_buf[BENCH_MAX_PAYLOAD + BENCH_OVERHEAD];
    uint8_t tx_buf[BENCH_MAX_PAYLOAD + BENCH_OVERHEAD];
    uint32_t consecutive_timeouts = 0;

    for (uint32_t i = 0; i < total; i++) {
        size_t rx_len = 0;
        int rc = adapter->recv(adapter, rx_buf, sizeof(rx_buf), &rx_len, cfg->timeout_ms);
        uint32_t rx_us = platform_timestamp_us();

        packet_result_t *pr = &res->results[res->result_count];
        pr->seq = (uint16_t)(i & 0xFFFF);
        pr->rx_us = rx_us;
        pr->is_warmup = (i < cfg->warmup) ? 1 : 0;

        if (rc == ADAPTER_ERR_TIMEOUT || rc == ADAPTER_ERR_RECV) {
            pr->lost = 1;
            res->packets_lost++;
            res->result_count++;
            consecutive_timeouts++;
            if (consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS) {
                platform_log("pp_rx: %u consecutive timeouts, aborting",
                             consecutive_timeouts);
                res->early_aborted = 1;
                break;
            }
            continue;
        }

        bench_packet_t pkt;
        int dec_rc = bench_packet_decode(rx_buf, rx_len, &pkt);
        if (dec_rc == -2) {
            pr->crc_ok = 0;
            res->crc_errors++;
        } else if (dec_rc == 0) {
            pr->crc_ok = 1;
        } else {
            pr->lost = 1;
            res->packets_lost++;
            res->result_count++;
            continue;
        }

        res->packets_received++;
        consecutive_timeouts = 0;

        /* Build and send PONG */
        pkt.msg_type = MSG_PONG;
        pkt.timestamp = platform_timestamp_us();
        int enc_len = bench_packet_encode(&pkt, tx_buf, sizeof(tx_buf));
        if (enc_len > 0) {
            adapter->send(adapter, tx_buf, (size_t)enc_len);
            res->packets_sent++;
        }

        pr->tx_us = pkt.timestamp;
        res->result_count++;
    }
}

/* ---- Flood sender ---- */

static void run_flood_sender(protocol_adapter_t *adapter,
                             const test_config_t *cfg,
                             test_results_t *res)
{
    uint32_t total = cfg->warmup + cfg->repetitions;
    uint8_t tx_buf[BENCH_MAX_PAYLOAD + BENCH_OVERHEAD];
    bench_packet_t pkt;
    int agg = cfg->aggregate_only;

    memset(&pkt, 0, sizeof(pkt));
    pkt.msg_type = MSG_FLOOD;
    for (uint16_t i = 0; i < cfg->payload_size; i++)
        pkt.payload[i] = (uint8_t)(i & 0xFF);
    pkt.payload_len = cfg->payload_size;

    for (uint32_t i = 0; i < total; i++) {
        pkt.seq_num = (uint16_t)(i & 0xFFFF);
        pkt.timestamp = platform_timestamp_us();

        int enc_len = bench_packet_encode(&pkt, tx_buf, sizeof(tx_buf));
        if (enc_len < 0)
            continue;

        uint32_t tx_us = platform_timestamp_us();
        int rc = adapter->send(adapter, tx_buf, (size_t)enc_len);
        res->packets_sent++;

        if (i == 0)
            res->start_us = tx_us;
        res->end_us = tx_us;

        if (rc != ADAPTER_OK)
            res->packets_lost++;

        if (!agg) {
            packet_result_t *pr = &res->results[res->result_count];
            pr->seq = pkt.seq_num;
            pr->tx_us = tx_us;
            pr->is_warmup = (i < cfg->warmup) ? 1 : 0;
            pr->lost = (rc != ADAPTER_OK) ? 1 : 0;
            res->result_count++;
        }

        if (cfg->inter_packet_us > 0)
            platform_sleep_us(cfg->inter_packet_us);
    }
}

/* ---- Flood receiver ---- */

static void run_flood_receiver(protocol_adapter_t *adapter,
                               const test_config_t *cfg,
                               test_results_t *res)
{
    uint32_t total = cfg->warmup + cfg->repetitions;
    uint8_t rx_buf[BENCH_MAX_PAYLOAD + BENCH_OVERHEAD];
    uint32_t consecutive_timeouts = 0;
    uint32_t highest_seq = 0;
    int agg = cfg->aggregate_only;

    /*
     * Drain approach: receive packets until we get several consecutive
     * timeouts (meaning the sender is done).  This avoids waiting the
     * full timeout_ms for every single lost packet.
     */
    for (uint32_t i = 0; i < total; i++) {
        size_t rx_len = 0;
        int rc = adapter->recv(adapter, rx_buf, sizeof(rx_buf), &rx_len, cfg->timeout_ms);
        uint32_t rx_us = platform_timestamp_us();

        if (rc == ADAPTER_ERR_TIMEOUT || rc == ADAPTER_ERR_RECV) {
            consecutive_timeouts++;
            /* After receiving at least one packet, stop on consecutive timeouts */
            if (res->packets_received > 0 &&
                consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS) {
                platform_log("flood_rx: %u consecutive timeouts after %u packets, stopping",
                             consecutive_timeouts, res->packets_received);
                res->early_aborted = 1;
                break;
            }
            continue;  /* Don't record timeout slots — we count loss from seq gaps */
        }

        consecutive_timeouts = 0;

        bench_packet_t pkt;
        int dec_rc = bench_packet_decode(rx_buf, rx_len, &pkt);

        if (dec_rc == -2) {
            res->crc_errors++;
        } else if (dec_rc != 0) {
            continue;  /* completely garbled — skip */
        }

        /* Timing bookkeeping */
        if (res->packets_received == 0)
            res->start_us = rx_us;
        res->end_us = rx_us;

        res->packets_received++;

        if (pkt.seq_num > highest_seq)
            highest_seq = pkt.seq_num;

        if (!agg) {
            packet_result_t *pr = &res->results[res->result_count];
            pr->is_warmup = (pkt.seq_num < cfg->warmup) ? 1 : 0;
            pr->seq = pkt.seq_num;
            pr->rx_us = rx_us;
            pr->crc_ok = (dec_rc == 0) ? 1 : 0;
            res->result_count++;
        }
    }

    /* Count lost packets: total sent (highest_seq+1) minus received */
    if (res->packets_received > 0 && highest_seq + 1 > res->packets_received)
        res->packets_lost = (highest_seq + 1) - res->packets_received;

    /* Send FLOOD_ACK summary */
    uint8_t tx_buf[BENCH_OVERHEAD];
    bench_packet_t ack;
    memset(&ack, 0, sizeof(ack));
    ack.msg_type = MSG_FLOOD_ACK;
    ack.seq_num = (uint16_t)(res->packets_received & 0xFFFF);
    ack.timestamp = platform_timestamp_us();
    ack.payload_len = 0;
    int enc_len = bench_packet_encode(&ack, tx_buf, sizeof(tx_buf));
    if (enc_len > 0)
        adapter->send(adapter, tx_buf, (size_t)enc_len);
}

/* ---- RSSI sampler ---- */

static void run_rssi_sampler(protocol_adapter_t *adapter,
                             const test_config_t *cfg,
                             test_results_t *res)
{
    uint32_t total = cfg->warmup + cfg->repetitions;
    uint32_t interval_ms = cfg->inter_packet_us / 1000;
    if (interval_ms == 0) interval_ms = 100;  /* default 100ms between samples */
    uint32_t consecutive_failures = 0;

    for (uint32_t i = 0; i < total; i++) {
        packet_result_t *pr = &res->results[res->result_count];
        pr->seq = (uint16_t)(i & 0xFFFF);
        pr->is_warmup = (i < cfg->warmup) ? 1 : 0;
        pr->rx_us = platform_timestamp_us();

        int rssi;
        if (adapter->get_rssi(adapter, &rssi) == ADAPTER_OK) {
            pr->rssi = rssi;
            pr->crc_ok = 1;  /* no payload to validate — mark as OK */
            res->packets_received++;
            consecutive_failures = 0;
        } else {
            pr->lost = 1;
            res->packets_lost++;
            consecutive_failures++;
            if (consecutive_failures >= MAX_CONSECUTIVE_TIMEOUTS) {
                platform_log("rssi: %u consecutive failures, aborting",
                             consecutive_failures);
                res->result_count++;
                res->early_aborted = 1;
                break;
            }
        }
        res->result_count++;

        platform_sleep_ms(interval_ms);
    }
}

/* ---- Public API ---- */

test_results_t *test_engine_run(protocol_adapter_t *adapter,
                                const test_config_t *cfg)
{
    if (!adapter || !cfg)
        return NULL;

    uint32_t total = cfg->warmup + cfg->repetitions;
    uint32_t alloc_count = cfg->aggregate_only ? 0 : total;
    test_results_t *res = test_results_alloc(alloc_count);
    if (!res)
        return NULL;

    res->warmup_count = cfg->warmup;
    res->aggregate_only = cfg->aggregate_only;

    switch (cfg->mode) {
    case TEST_MODE_PING_PONG:
        if (cfg->role == ROLE_SENDER)
            run_ping_pong_sender(adapter, cfg, res);
        else
            run_ping_pong_receiver(adapter, cfg, res);
        break;
    case TEST_MODE_FLOOD:
        if (cfg->role == ROLE_SENDER)
            run_flood_sender(adapter, cfg, res);
        else
            run_flood_receiver(adapter, cfg, res);
        break;
    case TEST_MODE_RSSI:
        run_rssi_sampler(adapter, cfg, res);
        break;
    default:
        test_results_free(res);
        return NULL;
    }

    return res;
}

/* ---- JSON serialization (snprintf-based) ---- */

char *test_results_to_json(const test_results_t *results,
                           const test_config_t *cfg)
{
    if (!results || !cfg)
        return NULL;

    /* Estimate buffer size: ~120 bytes per packet + 512 for metadata */
    size_t buf_size = 512 + results->result_count * 128;
    char *buf = malloc(buf_size);
    if (!buf)
        return NULL;

    int off = 0;
    off += snprintf(buf + off, buf_size - off,
        "{\n"
        "  \"mode\": \"%s\",\n"
        "  \"role\": \"%s\",\n"
        "  \"payload_size\": %u,\n"
        "  \"repetitions\": %u,\n"
        "  \"warmup\": %u,\n"
        "  \"packets_sent\": %u,\n"
        "  \"packets_received\": %u,\n"
        "  \"packets_lost\": %u,\n"
        "  \"crc_errors\": %u,\n"
        "  \"start_us\": %u,\n"
        "  \"end_us\": %u,\n"
        "  \"aggregate_only\": %u,\n"
        "  \"early_aborted\": %u,\n"
        "  \"packets\": [\n",
        cfg->mode == TEST_MODE_PING_PONG ? "ping_pong" :
        cfg->mode == TEST_MODE_FLOOD     ? "flood"     : "rssi",
        cfg->role == ROLE_SENDER ? "sender" : "receiver",
        cfg->payload_size,
        cfg->repetitions,
        cfg->warmup,
        results->packets_sent,
        results->packets_received,
        results->packets_lost,
        results->crc_errors,
        results->start_us,
        results->end_us,
        results->aggregate_only,
        results->early_aborted);

    for (uint32_t i = 0; i < results->result_count; i++) {
        const packet_result_t *p = &results->results[i];

        /* Grow buffer if needed */
        if ((size_t)(off + 256) >= buf_size) {
            buf_size *= 2;
            char *nb = realloc(buf, buf_size);
            if (!nb) { free(buf); return NULL; }
            buf = nb;
        }

        off += snprintf(buf + off, buf_size - off,
            "    {\"seq\": %u, \"tx_us\": %u, \"rx_us\": %u, "
            "\"rtt_us\": %u, \"rssi\": %d, \"crc_ok\": %u, "
            "\"lost\": %u, \"warmup\": %u}%s\n",
            p->seq, p->tx_us, p->rx_us,
            p->rtt_us, p->rssi, p->crc_ok,
            p->lost, p->is_warmup,
            (i + 1 < results->result_count) ? "," : "");
    }

    off += snprintf(buf + off, buf_size - off, "  ]\n}\n");
    return buf;
}

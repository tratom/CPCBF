/*
 * CPCBF — LoRa Adapter for Arduino MKR WAN 1300
 * Implements protocol_adapter_t over Sandeep Mistry's LoRa library.
 *
 * Topology: raw point-to-point broadcast (no addressing).
 *   Both boards run identical init. Half-duplex: after every send,
 *   re-enter RX so recv() can drain the reply.
 *
 * RSSI semantics: LoRa has no ambient/connection RSSI (unlike WiFi or BLE).
 * The radio only reports RSSI for packets it actually received. To match
 * the adapter contract (get_rssi() returns a meaningful value every call
 * in RSSI mode), get_rssi() runs a tiny probe ping-pong:
 *   - sender role: TXes a 1-byte probe, waits up to PROBE_REPLY_MS for a
 *     reply, returns LoRa.packetRssi() of the reply.
 *   - receiver role: waits up to PROBE_WAIT_MS for a probe, TXes a 1-byte
 *     reply, returns LoRa.packetRssi() of the probe.
 * Both sides thus measure the counter-party's signal — symmetric data.
 * For ping_pong / flood modes the probe path is unused.
 */
#include <Arduino.h>
#include <LoRa.h>

extern "C" {
#include "protocol_adapter.h"
#include "platform_hal.h"
}

#include "config.h"

#define PROBE_WAIT_MS     2000   /* receiver's inbound probe timeout */
#define PROBE_REPLY_MS    1500   /* sender's inbound reply timeout — symmetric
                                   with PROBE_WAIT_MS so TX→RX turnaround
                                   jitter doesn't cost reply packets */
#define PROBE_MAGIC       0xA5

static struct {
    adapter_config_t cfg;
    int last_rssi;
    bool initialized;
} s_lora = {{}, 0, false};

static int recv_with_timeout(uint8_t *buf, size_t buf_len,
                             size_t *out_len, uint32_t timeout_ms)
{
    uint32_t start = millis();
    while ((millis() - start) < timeout_ms) {
        int pkt_size = LoRa.parsePacket();
        if (pkt_size > 0) {
            size_t to_read = (size_t)pkt_size < buf_len ? (size_t)pkt_size : buf_len;
            size_t n = 0;
            while (n < to_read && LoRa.available())
                buf[n++] = (uint8_t)LoRa.read();
            while (LoRa.available()) (void)LoRa.read();  /* drop overflow */
            s_lora.last_rssi = LoRa.packetRssi();
            *out_len = n;
            return ADAPTER_OK;
        }
        delay(1);
    }
    *out_len = 0;
    return ADAPTER_ERR_TIMEOUT;
}

static int lora_init(protocol_adapter_t *self, const adapter_config_t *cfg)
{
    (void)self;
    memcpy(&s_lora.cfg, cfg, sizeof(adapter_config_t));
    s_lora.initialized = false;
    s_lora.last_rssi = 0;

    /* Honour runtime overrides from the plan; fall back to compile-time
     * defaults in config.h when the controller didn't set them. LDRO is
     * auto-managed by the Sandeep LoRa lib inside setSignalBandwidth(). */
    int      sf  = (cfg->lora_sf  != 0)            ? cfg->lora_sf  : LORA_SF;
    long     bw  = (cfg->lora_bw_hz != 0)          ? (long)cfg->lora_bw_hz : (long)LORA_BW;
    int      cr  = (cfg->lora_cr  != 0)            ? cfg->lora_cr  : LORA_CR;
    int      pwr = (cfg->lora_tx_power_dbm != LORA_TX_POWER_UNSET)
                       ? cfg->lora_tx_power_dbm : LORA_TX_POWER_DBM;

    platform_log("lora: begin freq=%ld SF=%d BW=%ld CR=4/%d pwr=%d dBm",
                 (long)LORA_FREQUENCY, sf, bw, cr, pwr);

    /* The LoRa library's begin() handles MKR WAN 1300 internally:
     * defaults to SPI1 + SS=LORA_IRQ_DUMB, and pulses LORA_RESET with
     * BOOT0 low to bring the Murata module up in normal boot. */
    if (!LoRa.begin(LORA_FREQUENCY)) {
        platform_log("lora: begin() failed");
        return ADAPTER_ERR_INIT;
    }

    LoRa.setSpreadingFactor(sf);
    LoRa.setSignalBandwidth(bw);
    LoRa.setCodingRate4(cr);
    LoRa.setPreambleLength(LORA_PREAMBLE);
    LoRa.setSyncWord(LORA_SYNC_WORD);
    LoRa.setTxPower(pwr);
    LoRa.enableCrc();

    /* MKR WAN 1300 lacks LoRa.receive() (DIO0 not routed externally on this
     * board). RX mode is engaged implicitly by the first LoRa.parsePacket()
     * call inside recv() / recv_with_timeout(). */
    s_lora.initialized = true;

    platform_log("lora: ready (role=%s)",
                 cfg->role == ROLE_SENDER ? "sender" : "receiver");
    return ADAPTER_OK;
}

static int lora_send(protocol_adapter_t *self, const uint8_t *data, size_t len)
{
    (void)self;
    if (!s_lora.initialized) return ADAPTER_ERR_SEND;

    LoRa.beginPacket();
    LoRa.write(data, len);
    int rc = LoRa.endPacket();      /* blocking: returns when TX complete */

    /* No explicit receive() on MKR WAN 1300 — parsePacket() toggles RX.
     * The radio idles after TX; the next recv() / parsePacket() drives it. */

    return rc ? ADAPTER_OK : ADAPTER_ERR_SEND;
}

static int lora_recv(protocol_adapter_t *self, uint8_t *buf, size_t buf_len,
                     size_t *out_len, uint32_t timeout_ms)
{
    (void)self;
    if (!s_lora.initialized) return ADAPTER_ERR_RECV;

    int rc = recv_with_timeout(buf, buf_len, out_len, timeout_ms);
    return rc == ADAPTER_OK ? ADAPTER_OK : ADAPTER_ERR_TIMEOUT;
}

static int lora_get_rssi(protocol_adapter_t *self, int *rssi_dbm)
{
    (void)self;
    if (!s_lora.initialized) return ADAPTER_ERR_RSSI;

    uint8_t probe[2] = { PROBE_MAGIC, 0 };
    uint8_t rxbuf[16];
    size_t rxlen = 0;

    if (s_lora.cfg.role == ROLE_SENDER) {
        /* Send probe, then wait briefly for the peer's reply. RSSI is
         * read from the reply. */
        LoRa.beginPacket();
        LoRa.write(probe, sizeof(probe));
        LoRa.endPacket();

        int rc = recv_with_timeout(rxbuf, sizeof(rxbuf), &rxlen, PROBE_REPLY_MS);
        if (rc != ADAPTER_OK) return ADAPTER_ERR_RSSI;
        *rssi_dbm = s_lora.last_rssi;
        return ADAPTER_OK;
    }

    /* Receiver: wait for probe, measure RSSI, send reply back. */
    int rc = recv_with_timeout(rxbuf, sizeof(rxbuf), &rxlen, PROBE_WAIT_MS);
    if (rc != ADAPTER_OK) return ADAPTER_ERR_RSSI;
    int probe_rssi = s_lora.last_rssi;

    LoRa.beginPacket();
    LoRa.write(probe, sizeof(probe));
    LoRa.endPacket();

    *rssi_dbm = probe_rssi;
    return ADAPTER_OK;
}

static int lora_deinit(protocol_adapter_t *self)
{
    (void)self;
    if (s_lora.initialized) {
        LoRa.end();
        s_lora.initialized = false;
        platform_log("lora: deinitialized");
    }
    return ADAPTER_OK;
}

extern "C" int lora_adapter_is_active(void)
{
    return s_lora.initialized ? 1 : 0;
}

extern "C" {
protocol_adapter_t lora_adapter = {
    .init     = lora_init,
    .send     = lora_send,
    .recv     = lora_recv,
    .get_rssi = lora_get_rssi,
    .deinit   = lora_deinit,
    .priv     = nullptr,
};
}

/*
 * CPCBF — WiFiNINA Adapter for MKR WiFi 1010
 * Implements protocol_adapter_t over WiFiNINA SoftAP/STA + WiFiUDP.
 *
 * Topology: SoftAP + STA
 *   Sender  = AP  (creates network, 192.168.4.1)
 *   Receiver = STA (joins network,  192.168.4.2)
 */
#include <Arduino.h>
#include <WiFiNINA.h>
#include <WiFiUdp.h>

extern "C" {
#include "protocol_adapter.h"
#include "platform_hal.h"
}

/* Static state — no malloc for private data */
static struct {
    WiFiUDP udp;
    adapter_config_t cfg;
    IPAddress peer_ip;
    bool initialized;
} s_nina;

static int nina_init(protocol_adapter_t *self, const adapter_config_t *cfg)
{
    (void)self;
    memcpy(&s_nina.cfg, cfg, sizeof(adapter_config_t));
    s_nina.initialized = false;

    const char *essid = cfg->essid[0] ? cfg->essid : "CPCBF_MKR";
    int channel = cfg->channel > 0 ? cfg->channel : 6;

    if (cfg->role == ROLE_SENDER) {
        /* AP mode */
        platform_log("nina: starting AP '%s' ch %d", essid, channel);
        int status = WiFi.beginAP(essid, channel);
        if (status != WL_AP_LISTENING) {
            platform_log("nina: AP start failed, status=%d", status);
            return ADAPTER_ERR_INIT;
        }
        /* AP IP is always 192.168.4.1 on WiFiNINA */
        s_nina.peer_ip.fromString(cfg->peer_addr[0] ? cfg->peer_addr : "192.168.4.2");
        platform_log("nina: AP started, peer=%s", cfg->peer_addr);
    } else {
        /* STA mode — join the AP */
        platform_log("nina: connecting to '%s'", essid);
        uint32_t start = millis();
        int status = WiFi.begin(essid);
        while (status != WL_CONNECTED && (millis() - start) < 30000) {
            delay(500);
            status = WiFi.status();
        }
        if (status != WL_CONNECTED) {
            platform_log("nina: STA connect failed, status=%d", status);
            return ADAPTER_ERR_INIT;
        }
        s_nina.peer_ip.fromString(cfg->peer_addr[0] ? cfg->peer_addr : "192.168.4.1");
        platform_log("nina: connected, local=%s peer=%s",
                      WiFi.localIP().toString().c_str(), cfg->peer_addr);
    }

    uint16_t port = cfg->port > 0 ? cfg->port : 5201;
    s_nina.udp.begin(port);
    s_nina.initialized = true;

    platform_log("nina: UDP bound to port %u", port);
    return ADAPTER_OK;
}

static int nina_send(protocol_adapter_t *self, const uint8_t *data, size_t len)
{
    (void)self;
    if (!s_nina.initialized) return ADAPTER_ERR_SEND;

    uint16_t port = s_nina.cfg.port > 0 ? s_nina.cfg.port : 5201;
    s_nina.udp.beginPacket(s_nina.peer_ip, port);
    s_nina.udp.write(data, len);
    int rc = s_nina.udp.endPacket();

    return rc ? ADAPTER_OK : ADAPTER_ERR_SEND;
}

static int nina_recv(protocol_adapter_t *self, uint8_t *buf, size_t buf_len,
                     size_t *out_len, uint32_t timeout_ms)
{
    (void)self;
    if (!s_nina.initialized) return ADAPTER_ERR_RECV;

    uint32_t start = millis();
    while ((millis() - start) < timeout_ms) {
        int pkt_size = s_nina.udp.parsePacket();
        if (pkt_size > 0) {
            size_t to_read = (size_t)pkt_size < buf_len ? (size_t)pkt_size : buf_len;
            int n = s_nina.udp.read(buf, to_read);
            if (n > 0) {
                *out_len = (size_t)n;
                return ADAPTER_OK;
            }
        }
        delay(1);
    }

    *out_len = 0;
    return ADAPTER_ERR_TIMEOUT;
}

static int nina_get_rssi(protocol_adapter_t *self, int *rssi_dbm)
{
    (void)self;
    long rssi = WiFi.RSSI();
    if (rssi == 0) return ADAPTER_ERR_RSSI;
    *rssi_dbm = (int)rssi;
    return ADAPTER_OK;
}

static int nina_deinit(protocol_adapter_t *self)
{
    (void)self;
    if (s_nina.initialized) {
        s_nina.udp.stop();
        WiFi.end();
        s_nina.initialized = false;
        platform_log("nina: deinitialized");
    }
    return ADAPTER_OK;
}

/* Global adapter instance */
extern "C" protocol_adapter_t wifi_nina_adapter = {
    .init     = nina_init,
    .send     = nina_send,
    .recv     = nina_recv,
    .get_rssi = nina_get_rssi,
    .deinit   = nina_deinit,
    .priv     = nullptr,
};

/*
 * CPCBF — ArduinoBLE Adapter for MKR WiFi 1010
 * Implements protocol_adapter_t over BLE GATT characteristics.
 *
 * Topology:
 *   Receiver = Peripheral (advertises service, accepts connection)
 *   Sender   = Central    (scans for service UUID, connects)
 *
 * Data flow:
 *   Sender→Receiver: Central writes to peripheral's TX characteristic
 *   Receiver→Sender: Peripheral writes to RX characteristic (notify)
 *   recv(): polls BLE.poll() checking rx_ready flag with millis() timeout
 */
#include <Arduino.h>
#include <ArduinoBLE.h>
#include "utility/ATT.h"
#include "utility/HCI.h"

extern "C" {
#include "protocol_adapter.h"
#include "platform_hal.h"
}

#include "config.h"

/* Ring buffer for received packets (ping-pong / RSSI modes) */
struct rx_slot {
    uint8_t data[BLE_MAX_ATT_PAYLOAD];
    size_t  len;
};

/* Linear byte pool for flood bursts.
 * BLE.poll() delivers ALL buffered NINA packets in one call — hundreds
 * of callbacks fire before recv() can consume any.  A small ring
 * overflows immediately.  This pool stores variable-size entries
 * (2-byte length + payload) and can hold many more packets:
 *   20-byte payloads → ~818 packets;  230-byte → ~77 packets. */
static uint8_t  rx_pool[BLE_RX_POOL_BYTES];
static volatile uint16_t rx_pool_wpos;   /* write position (producer/callback) */
static uint16_t          rx_pool_rpos;   /* read position  (consumer/recv)     */
static volatile uint16_t rx_pool_count;  /* packets stored */

/* Static state — no malloc */
static struct {
    BLEService service{BLE_SERVICE_UUID};
    BLECharacteristic txChar{BLE_TX_CHAR_UUID,
                             BLEWrite | BLEWriteWithoutResponse,
                             BLE_MAX_ATT_PAYLOAD};
    BLECharacteristic rxChar{BLE_RX_CHAR_UUID,
                             BLERead | BLENotify,
                             BLE_MAX_ATT_PAYLOAD};
    adapter_config_t cfg;
    bool initialized;
    /* Ring buffer receive queue */
    rx_slot ring[BLE_RX_RING_SLOTS];
    volatile uint8_t ring_head;   /* next slot to write (producer) */
    volatile uint8_t ring_tail;   /* next slot to read  (consumer) */
    /* Connected device (central role) */
    BLEDevice peer;
    BLECharacteristic remote_tx; /* remote's TX char (central writes here) */
    BLECharacteristic remote_rx; /* remote's RX char (central reads/subscribes) */
} s_ble;

/* Error detail for JSON response — set by init functions */
static const char *s_ble_error_detail = nullptr;

/* Persistent BLE stack flag.  ArduinoBLE has a use-after-free in its
 * end()/begin() cycle (GATT::end deletes generic services but leaves
 * dangling pointers in _attributes; the next begin→clearAttributes
 * dereferences them).  We work around this by calling BLE.begin() and
 * registering the GATT table exactly once, then just reconnecting
 * between tests without tearing down the stack. */
static bool s_ble_stack_active = false;

/* Persistent GATT-table flag.  Set only on the peripheral side — the
 * central must never register a local GATT table, because doing so
 * puts the NINA into a dual-role (central+peripheral) config that
 * blocks remote notification delivery and hangs ping-pong recv(). */
static bool s_ble_gatt_registered = false;

/* ---- Ring buffer helpers ---- */

static inline bool ring_full(void)
{
    return ((s_ble.ring_head + 1) % BLE_RX_RING_SLOTS) == s_ble.ring_tail;
}

static inline bool ring_empty(void)
{
    return s_ble.ring_head == s_ble.ring_tail;
}

static void ring_push(const uint8_t *data, size_t len)
{
    if (ring_full()) return;          /* drop oldest-unread is the alternative,
                                         but dropping newest is simpler and fine
                                         for benchmarking — the loss is counted */
    rx_slot *slot = &s_ble.ring[s_ble.ring_head];
    if (len > BLE_MAX_ATT_PAYLOAD) len = BLE_MAX_ATT_PAYLOAD;
    memcpy(slot->data, data, len);
    slot->len = len;
    s_ble.ring_head = (s_ble.ring_head + 1) % BLE_RX_RING_SLOTS;
}

/* ---- Diagnostic counters ---- */
static volatile uint32_t s_callback_count = 0;
static volatile uint32_t s_pool_push_count = 0;
static volatile uint32_t s_pool_full_count = 0;

/* ---- Pool helpers ---- */

static void pool_reset(void)
{
    rx_pool_wpos  = 0;
    rx_pool_rpos  = 0;
    rx_pool_count = 0;
}

static void pool_push(const uint8_t *data, size_t len)
{
    if (len > BLE_MAX_ATT_PAYLOAD) len = BLE_MAX_ATT_PAYLOAD;
    uint16_t needed = 2 + (uint16_t)len;
    if (rx_pool_wpos + needed > BLE_RX_POOL_BYTES) {
        s_pool_full_count++;
        return;   /* pool exhausted */
    }
    rx_pool[rx_pool_wpos]     = (uint8_t)(len & 0xFF);
    rx_pool[rx_pool_wpos + 1] = (uint8_t)((len >> 8) & 0xFF);
    memcpy(&rx_pool[rx_pool_wpos + 2], data, len);
    rx_pool_wpos += needed;
    rx_pool_count++;
    s_pool_push_count++;
}

static bool pool_empty(void)
{
    return rx_pool_rpos >= rx_pool_wpos;
}

static bool pool_pop(uint8_t *buf, size_t buf_len, size_t *out_len)
{
    if (pool_empty()) return false;
    uint16_t len = rx_pool[rx_pool_rpos] | ((uint16_t)rx_pool[rx_pool_rpos + 1] << 8);
    size_t to_copy = len < buf_len ? len : buf_len;
    memcpy(buf, &rx_pool[rx_pool_rpos + 2], to_copy);
    *out_len = to_copy;
    rx_pool_rpos += 2 + len;
    rx_pool_count--;

    /* Reclaim buffer when fully drained — allows reuse for next burst */
    if (pool_empty()) {
        rx_pool_wpos = 0;
        rx_pool_rpos = 0;
    }

    return true;
}

/* ---- Characteristic event handlers ---- */

static void on_tx_written(BLEDevice central, BLECharacteristic characteristic)
{
    (void)central;
    s_callback_count++;
    pool_push(characteristic.value(), characteristic.valueLength());
}

static void on_rx_notify(BLEDevice peripheral, BLECharacteristic characteristic)
{
    (void)peripheral;
    ring_push(characteristic.value(), characteristic.valueLength());
}

/* ---- Peripheral (Receiver) init ---- */

static void ble_prepare(void)
{
    /* BLE-only firmware: no WiFiNINA to contend for the SPI bus.
     * NINA module SPI is exclusively used for BLE. */
    delay(100);
    platform_log("ble: preparing BLE (dedicated firmware)");
}

/* Initialize the BLE stack (BLE.begin) exactly once per power cycle.
 * Idempotent thanks to s_ble_stack_active.  Safe to call from any
 * role — does NOT touch the GATT attribute table.  Callers that need
 * a local GATT table (peripheral only) must also call
 * ble_nina_register_gatt(). */
extern "C" int ble_nina_ensure_stack(void)
{
    if (s_ble_stack_active) return ADAPTER_OK;

    ble_prepare();
    if (!BLE.begin()) {
        platform_log("ble: BLE.begin() failed — check NINA firmware supports BLE over SPI");
        s_ble_error_detail = "BLE.begin() failed";
        return ADAPTER_ERR_INIT;
    }
    platform_log("ble: BLE.begin() ok, address: %s", BLE.address().c_str());

    s_ble_stack_active = true;
    return ADAPTER_OK;
}

/* Register the local GATT table (service + TX/RX chars + write handler).
 * Peripheral-only.  The central must NOT call this — registering a
 * local service with the same UUIDs as the remote ones it subscribes
 * to puts the NINA in a dual-role config that blocks notify delivery. */
static void ble_nina_register_gatt(void)
{
    if (s_ble_gatt_registered) return;

    BLE.setLocalName("CPCBF_BLE");
    BLE.setAdvertisedService(s_ble.service);
    s_ble.service.addCharacteristic(s_ble.txChar);
    s_ble.service.addCharacteristic(s_ble.rxChar);
    BLE.addService(s_ble.service);
    s_ble.txChar.setEventHandler(BLEWritten, on_tx_written);

    s_ble_gatt_registered = true;
}

/* Return the local NINA BLE address.  Caller must have called
 * ble_nina_ensure_stack() first (returns "00:00:00:00:00:00" otherwise). */
extern "C" const char *ble_nina_local_addr(void)
{
    static char addr_buf[24];
    String addr = BLE.address();
    strncpy(addr_buf, addr.c_str(), sizeof(addr_buf) - 1);
    addr_buf[sizeof(addr_buf) - 1] = '\0';
    return addr_buf;
}

static int ble_init_peripheral(void)
{
    platform_log("ble: starting peripheral (receiver)");

    int rc = ble_nina_ensure_stack();
    if (rc != ADAPTER_OK) {
        s_ble_error_detail = "BLE.begin() failed (peripheral)";
        return rc;
    }

    ble_nina_register_gatt();

    BLE.setAdvertisingInterval(BLE_ADV_INTERVAL);
    BLE.advertise();
    platform_log("ble: advertising, waiting for central...");

    /* Wait for central to connect */
    uint32_t start = millis();
    while (!BLE.central()) {
        BLE.poll();
        if ((millis() - start) > BLE_CONNECT_TIMEOUT_MS) {
            platform_log("ble: connection timeout — central never connected");
            s_ble_error_detail = "peripheral timeout waiting for central";
            return ADAPTER_ERR_TIMEOUT;
        }
    }

    s_ble.peer = BLE.central();
    platform_log("ble: central connected: %s", s_ble.peer.address().c_str());

    /* Let the NINA settle after connection: process LE_Connection_Complete
     * and any LL control PDUs before the test loop starts issuing HCI
     * Read_RSSI / ATT writes.  Without this, .rssi() on a not-yet-registered
     * connection handle can block the HCI command channel. */
    for (int i = 0; i < 10; i++) { BLE.poll(); delay(50); }

    return ADAPTER_OK;
}

/* Helper to construct a BLEDevice from a known address.
 * The BLEDevice(addressType, address) ctor is protected;
 * this thin subclass makes it accessible. */
class BLEKnownDevice : public BLEDevice {
public:
    BLEKnownDevice(uint8_t addrType, uint8_t addr[6]) : BLEDevice(addrType, addr) {}
};

/* ---- Address parsing helper ---- */

/* Parse "AA:BB:CC:DD:EE:FF" into ArduinoBLE's internal byte order (LSB first) */
static bool parse_ble_addr(const char *str, uint8_t out[6])
{
    unsigned int b[6];
    if (sscanf(str, "%02x:%02x:%02x:%02x:%02x:%02x",
               &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6)
        return false;
    for (int i = 0; i < 6; i++)
        out[5 - i] = (uint8_t)b[i];
    return true;
}

/* ---- Central (Sender) init ---- */

static int ble_init_central(void)
{
    platform_log("ble: starting central (sender)");

    if (ble_nina_ensure_stack() != ADAPTER_OK)
        return ADAPTER_ERR_INIT;

    BLEDevice peripheral;

    /* If the inventory supplied the peripheral's BLE MAC via peer_mac,
     * use HCI LE Create Connection directly — much more reliable than
     * ArduinoBLE's active scan + discovered() check.  Otherwise fall
     * back to UUID-based scanning. */
    if (s_ble.cfg.peer_mac[0] != '\0') {
        uint8_t addr[6];
        if (!parse_ble_addr(s_ble.cfg.peer_mac, addr)) {
            platform_log("ble: invalid peer_mac: %s", s_ble.cfg.peer_mac);
            s_ble_error_detail = "bad peer_mac";
            return ADAPTER_ERR_INIT;
        }
        platform_log("ble: direct connect to %s", s_ble.cfg.peer_mac);
        peripheral = BLEKnownDevice(0x00, addr);  /* 0x00 = public address */
    } else {
        /* ---- Scan-based discovery (fallback) ---- */
        bool found = false;
        for (int round = 0; round < BLE_SCAN_MAX_ROUNDS; round++) {
            BLE.stopScan();
            platform_log("ble: scan round %d/%d for service %s",
                         round + 1, BLE_SCAN_MAX_ROUNDS, BLE_SERVICE_UUID);
            BLE.scanForUuid(BLE_SERVICE_UUID);

            uint32_t round_start = millis();
            while ((millis() - round_start) < BLE_SCAN_ROUND_MS) {
                peripheral = BLE.available();
                if (peripheral) { found = true; break; }
            }
            if (found) break;
            platform_log("ble: scan round %d — not found, restarting...", round + 1);
        }
        BLE.stopScan();
        if (!found) {
            platform_log("ble: scan timeout — peripheral not found after %d rounds",
                         BLE_SCAN_MAX_ROUNDS);
            s_ble_error_detail = "scan timeout";
            return ADAPTER_ERR_TIMEOUT;
        }
        platform_log("ble: found peripheral: %s", peripheral.address().c_str());
    }

    if (!peripheral.connect()) {
        platform_log("ble: connection failed");
        s_ble_error_detail = "connect() failed";
        return ADAPTER_ERR_INIT;
    }

    platform_log("ble: connected, flushing HCI events...");

    /* Flush pending HCI events — the connection complete event must be
     * fully processed before ATT operations can work */
    for (int i = 0; i < 20; i++) {
        BLE.poll();
        delay(100);
    }

    /* ---- Request Data Length Extension (DLE) ----
     * ArduinoBLE doesn't negotiate DLE, so LL data length stays at the
     * BLE 4.0 default of 27 bytes.  A 230-byte write fragments into ~9
     * LL PDUs — fragment loss under flood kills the entire packet.
     * Sending HCI LE_Set_Data_Length (OGF=0x08, OCF=0x0022) requests
     * 251-byte LL PDUs, eliminating fragmentation.
     * Build the LE_Set_Data_Length HCI command:
     *   Connection_Handle (2B) + TxOctets (2B) + TxTime (2B)          */
    {
        /* The connection handle is the first one registered in ATT.
         * Since we have exactly one connection, we can get it via
         * the peer's address bytes.  */
        uint8_t peerAddr[6];
        const char *addrStr = peripheral.address().c_str();
        unsigned int b[6];
        if (sscanf(addrStr, "%02x:%02x:%02x:%02x:%02x:%02x",
                   &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) == 6) {
            for (int i = 0; i < 6; i++) peerAddr[5-i] = (uint8_t)b[i];
            uint16_t connHandle = ATT.connectionHandle(0x00, peerAddr);
            if (connHandle != 0xFFFF) {
                struct __attribute__((packed)) {
                    uint16_t handle;
                    uint16_t txOctets;
                    uint16_t txTime;
                } dleParams = { connHandle, 251, 2120 };
                /* OGF=0x08 (LE Controller), OCF=0x0022 → opcode = 0x2022 */
                int rc = HCI.sendCommand(0x2022, sizeof(dleParams), &dleParams);
                platform_log("ble: DLE request (251 octets) → %s",
                             rc == 0 ? "ok" : "failed");
                /* Flush the DLE response event */
                for (int i = 0; i < 5; i++) { BLE.poll(); delay(50); }
            } else {
                platform_log("ble: DLE skipped — no connection handle");
            }
        }
    }

    platform_log("ble: discovering service %s", BLE_SERVICE_UUID);
    if (!peripheral.discoverService(BLE_SERVICE_UUID)) {
        platform_log("ble: targeted discovery failed, trying full discovery...");
        if (!peripheral.discoverAttributes()) {
            platform_log("ble: full attribute discovery also failed");
            s_ble_error_detail = "discoverAttributes() failed";
            peripheral.disconnect();
            return ADAPTER_ERR_INIT;
        }
    }

    /* Find TX and RX characteristics on the remote peripheral */
    s_ble.remote_tx = peripheral.characteristic(BLE_TX_CHAR_UUID);
    s_ble.remote_rx = peripheral.characteristic(BLE_RX_CHAR_UUID);

    if (!s_ble.remote_tx || !s_ble.remote_rx) {
        platform_log("ble: chars not found tx=%d rx=%d",
                     (int)(bool)s_ble.remote_tx, (int)(bool)s_ble.remote_rx);
        s_ble_error_detail = "characteristics not found";
        peripheral.disconnect();
        return ADAPTER_ERR_INIT;
    }

    /* Subscribe to RX notifications from peripheral */
    if (s_ble.remote_rx.canSubscribe()) {
        s_ble.remote_rx.setEventHandler(BLEUpdated, on_rx_notify);
        if (!s_ble.remote_rx.subscribe()) {
            platform_log("ble: subscribe failed");
            s_ble_error_detail = "subscribe() failed";
            peripheral.disconnect();
            return ADAPTER_ERR_INIT;
        }
        platform_log("ble: subscribed to RX notifications");
    }

    s_ble.peer = peripheral;
    platform_log("ble: central init complete");
    return ADAPTER_OK;
}

/* ---- protocol_adapter_t interface ---- */

static int ble_nina_init(protocol_adapter_t *self, const adapter_config_t *cfg)
{
    (void)self;
    memcpy(&s_ble.cfg, cfg, sizeof(adapter_config_t));
    s_ble.initialized = false;
    s_ble.ring_head = 0;
    s_ble.ring_tail = 0;
    pool_reset();
    s_ble_error_detail = nullptr;

    int rc;
    if (cfg->role == ROLE_RECEIVER)
        rc = ble_init_peripheral();
    else
        rc = ble_init_central();

    if (rc == ADAPTER_OK)
        s_ble.initialized = true;
    return rc;
}

/* Expose last error detail for main.cpp to include in JSON response */
extern "C" const char *ble_nina_last_error(void)
{
    return s_ble_error_detail ? s_ble_error_detail : "unknown";
}

static int ble_nina_send(protocol_adapter_t *self, const uint8_t *data, size_t len)
{
    (void)self;
    if (!s_ble.initialized) return ADAPTER_ERR_SEND;

    /* Fast-path: if connection already dropped, don't enter BLE stack */
    if (s_ble.cfg.role == ROLE_RECEIVER) {
        BLEDevice central = BLE.central();
        if (!central || !central.connected())
            return ADAPTER_ERR_SEND;
    } else {
        if (!s_ble.peer.connected())
            return ADAPTER_ERR_SEND;
    }

    if (len > BLE_MAX_ATT_PAYLOAD) len = BLE_MAX_ATT_PAYLOAD;

    if (s_ble.cfg.role == ROLE_RECEIVER) {
        /* Peripheral: write to local RX char → notify central */
        if (!s_ble.rxChar.writeValue(data, len))
            return ADAPTER_ERR_SEND;
    } else {
        /* Central: write to remote TX char (write-without-response) */
        if (!s_ble.remote_tx.writeValue(data, len, false))
            return ADAPTER_ERR_SEND;
    }

    /* Process pending HCI events after each write.  In flood mode the
     * sender never calls recv(), so without this the BLE stack never
     * processes connection-maintenance events (supervision timeout
     * keep-alives, parameter updates, etc.) and the link drops. */
    BLE.poll();

    return ADAPTER_OK;
}

static int ble_nina_recv(protocol_adapter_t *self, uint8_t *buf, size_t buf_len,
                         size_t *out_len, uint32_t timeout_ms)
{
    (void)self;
    if (!s_ble.initialized) return ADAPTER_ERR_RECV;

    /* Fast path: data already in pool from a previous BLE.poll() burst */
    if (pool_pop(buf, buf_len, out_len))
        return ADAPTER_OK;

    uint32_t start = millis();
    while ((millis() - start) < timeout_ms) {
        /* Reclaim pool space before the next BLE.poll() burst.
         * pool_pop()'s compaction rarely fires because BLE.poll()
         * refills the pool before the last entry is consumed.
         * Resetting here guarantees the full 14 KB is available. */
        if (pool_empty()) {
            rx_pool_wpos = 0;
            rx_pool_rpos = 0;
        }

        BLE.poll();

        if (pool_pop(buf, buf_len, out_len))
            return ADAPTER_OK;

        /* Also check the ring buffer (used by on_rx_notify for central) */
        if (!ring_empty()) {
            rx_slot *slot = &s_ble.ring[s_ble.ring_tail];
            size_t to_copy = slot->len < buf_len ? slot->len : buf_len;
            memcpy(buf, slot->data, to_copy);
            *out_len = to_copy;
            s_ble.ring_tail = (s_ble.ring_tail + 1) % BLE_RX_RING_SLOTS;
            return ADAPTER_OK;
        }

        /* Bail immediately if BLE connection dropped */
        if (s_ble.cfg.role == ROLE_RECEIVER) {
            BLEDevice central = BLE.central();
            if (!central || !central.connected()) {
                *out_len = 0;
                return ADAPTER_ERR_RECV;
            }
        } else {
            if (!s_ble.peer.connected()) {
                *out_len = 0;
                return ADAPTER_ERR_RECV;
            }
        }

        delay(10);  /* Give NINA 10ms to process BLE radio RX between SPI polls.
                     * 2ms suffices for small packets but 220B+ packets need more
                     * time for LL fragment reassembly on the NINA (ESP32). */
    }

    *out_len = 0;
    return ADAPTER_ERR_TIMEOUT;
}

static int ble_nina_get_rssi(protocol_adapter_t *self, int *rssi_dbm)
{
    (void)self;
    if (!s_ble.initialized) return ADAPTER_ERR_RSSI;

    int rssi;
    if (s_ble.cfg.role == ROLE_RECEIVER) {
        BLEDevice central = BLE.central();
        if (!central) return ADAPTER_ERR_RSSI;
        rssi = central.rssi();
    } else {
        if (!s_ble.peer.connected()) return ADAPTER_ERR_RSSI;
        rssi = s_ble.peer.rssi();
    }

    if (rssi == 0 || rssi == 127) return ADAPTER_ERR_RSSI;
    *rssi_dbm = rssi;
    return ADAPTER_OK;
}

static int ble_nina_deinit(protocol_adapter_t *self)
{
    (void)self;
    if (s_ble.initialized) {
        if (s_ble.peer.connected())
            s_ble.peer.disconnect();
        /* Do NOT call BLE.end() — ArduinoBLE has a use-after-free in
         * end()/begin() that corrupts the GATT attribute list.  Keep
         * the stack active so the next test can just reconnect. */
        s_ble.initialized = false;
        s_ble.ring_head = 0;
        s_ble.ring_tail = 0;
        platform_log("ble: deinit — callback=%lu push=%lu full=%lu pool=%u/%uB",
                     (unsigned long)s_callback_count,
                     (unsigned long)s_pool_push_count,
                     (unsigned long)s_pool_full_count,
                     (unsigned)rx_pool_wpos,
                     (unsigned)BLE_RX_POOL_BYTES);
        s_callback_count = 0;
        s_pool_push_count = 0;
        s_pool_full_count = 0;
        pool_reset();
    }
    return ADAPTER_OK;
}

/* Full shutdown — only for RADIO_DISABLE when the user explicitly
 * wants to tear down the BLE stack. */
extern "C" void ble_nina_shutdown(void)
{
    if (s_ble.initialized) {
        if (s_ble.peer.connected())
            s_ble.peer.disconnect();
        s_ble.initialized = false;
    }
    if (s_ble_stack_active) {
        BLE.end();
        s_ble_stack_active = false;
        s_ble_gatt_registered = false;
        platform_log("ble: full shutdown (stack torn down)");
    }
    s_ble.ring_head = 0;
    s_ble.ring_tail = 0;
    pool_reset();
}

/* Poll BLE HCI events — call from main loop to keep the stack
 * responsive between tests (GATT discovery, notifications, etc.). */
extern "C" void ble_nina_poll(void)
{
    if (s_ble_stack_active)
        BLE.poll();
}

/* Global adapter instance */
extern "C" protocol_adapter_t ble_nina_adapter = {
    .init     = ble_nina_init,
    .send     = ble_nina_send,
    .recv     = ble_nina_recv,
    .get_rssi = ble_nina_get_rssi,
    .deinit   = ble_nina_deinit,
    .priv     = nullptr,
};

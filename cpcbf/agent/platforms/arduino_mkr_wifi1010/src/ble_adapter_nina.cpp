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
#include <WiFiNINA.h>
#include <ArduinoBLE.h>

extern "C" {
#include "protocol_adapter.h"
#include "platform_hal.h"
}

#include "config.h"

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
    /* Single-slot receive buffer */
    uint8_t rx_buf[BLE_MAX_ATT_PAYLOAD];
    size_t rx_len;
    volatile bool rx_ready;
    /* Connected device (central role) */
    BLEDevice peer;
    BLECharacteristic remote_tx; /* remote's TX char (central writes here) */
    BLECharacteristic remote_rx; /* remote's RX char (central reads/subscribes) */
} s_ble;

/* ---- Characteristic event handlers ---- */

static void on_tx_written(BLEDevice central, BLECharacteristic characteristic)
{
    /* Peripheral side: data arrived from central via TX char */
    (void)central;
    size_t len = characteristic.valueLength();
    if (len > BLE_MAX_ATT_PAYLOAD) len = BLE_MAX_ATT_PAYLOAD;
    memcpy(s_ble.rx_buf, characteristic.value(), len);
    s_ble.rx_len = len;
    s_ble.rx_ready = true;
}

static void on_rx_notify(BLEDevice peripheral, BLECharacteristic characteristic)
{
    /* Central side: notification arrived from peripheral via RX char */
    (void)peripheral;
    size_t len = characteristic.valueLength();
    if (len > BLE_MAX_ATT_PAYLOAD) len = BLE_MAX_ATT_PAYLOAD;
    memcpy(s_ble.rx_buf, characteristic.value(), len);
    s_ble.rx_len = len;
    s_ble.rx_ready = true;
}

/* ---- Peripheral (Receiver) init ---- */

static void ble_prepare(void)
{
    /* Firmware 3.x: BLE uses SPI (same as WiFi), not UART.
     * Stop WiFi to avoid interference during BLE benchmarks,
     * but keep SPI active — BLE needs it. */
    WiFi.end();
    delay(100);
    platform_log("ble: WiFi stopped, preparing BLE over SPI");
}

static int ble_init_peripheral(void)
{
    platform_log("ble: starting peripheral (receiver)");
    ble_prepare();

    if (!BLE.begin()) {
        platform_log("ble: BLE.begin() failed");
        return ADAPTER_ERR_INIT;
    }

    BLE.setLocalName("CPCBF_BLE");
    s_ble.service.addCharacteristic(s_ble.txChar);
    s_ble.service.addCharacteristic(s_ble.rxChar);
    BLE.addService(s_ble.service);

    /* Set write handler on TX char (incoming data from central) */
    s_ble.txChar.setEventHandler(BLEWritten, on_tx_written);

    BLE.advertise();
    platform_log("ble: advertising, waiting for central...");

    /* Wait for central to connect */
    uint32_t start = millis();
    while (!BLE.central()) {
        BLE.poll();
        if ((millis() - start) > BLE_CONNECT_TIMEOUT_MS) {
            platform_log("ble: connection timeout");
            BLE.end();
            return ADAPTER_ERR_TIMEOUT;
        }
    }

    s_ble.peer = BLE.central();
    platform_log("ble: central connected: %s", s_ble.peer.address().c_str());
    return ADAPTER_OK;
}

/* ---- Central (Sender) init ---- */

static int ble_init_central(void)
{
    platform_log("ble: starting central (sender)");
    ble_prepare();

    if (!BLE.begin()) {
        platform_log("ble: BLE.begin() failed");
        return ADAPTER_ERR_INIT;
    }

    /* Scan for peripheral advertising our service UUID */
    platform_log("ble: scanning for service %s", BLE_SERVICE_UUID);
    BLE.scanForUuid(BLE_SERVICE_UUID);

    BLEDevice peripheral;
    uint32_t start = millis();
    while (true) {
        peripheral = BLE.available();
        if (peripheral) break;
        if ((millis() - start) > BLE_CONNECT_TIMEOUT_MS) {
            platform_log("ble: scan timeout");
            BLE.stopScan();
            BLE.end();
            return ADAPTER_ERR_TIMEOUT;
        }
    }
    BLE.stopScan();

    platform_log("ble: found peripheral: %s", peripheral.address().c_str());

    if (!peripheral.connect()) {
        platform_log("ble: connection failed");
        BLE.end();
        return ADAPTER_ERR_INIT;
    }

    platform_log("ble: connected, discovering attributes...");
    if (!peripheral.discoverAttributes()) {
        platform_log("ble: attribute discovery failed");
        peripheral.disconnect();
        BLE.end();
        return ADAPTER_ERR_INIT;
    }

    /* Find TX and RX characteristics on the remote peripheral */
    s_ble.remote_tx = peripheral.characteristic(BLE_TX_CHAR_UUID);
    s_ble.remote_rx = peripheral.characteristic(BLE_RX_CHAR_UUID);

    if (!s_ble.remote_tx || !s_ble.remote_rx) {
        platform_log("ble: characteristics not found");
        peripheral.disconnect();
        BLE.end();
        return ADAPTER_ERR_INIT;
    }

    /* Subscribe to RX notifications from peripheral */
    if (s_ble.remote_rx.canSubscribe()) {
        s_ble.remote_rx.setEventHandler(BLEUpdated, on_rx_notify);
        if (!s_ble.remote_rx.subscribe()) {
            platform_log("ble: subscribe failed");
            peripheral.disconnect();
            BLE.end();
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
    s_ble.rx_ready = false;
    s_ble.rx_len = 0;

    int rc;
    if (cfg->role == ROLE_RECEIVER)
        rc = ble_init_peripheral();
    else
        rc = ble_init_central();

    if (rc == ADAPTER_OK)
        s_ble.initialized = true;
    return rc;
}

static int ble_nina_send(protocol_adapter_t *self, const uint8_t *data, size_t len)
{
    (void)self;
    if (!s_ble.initialized) return ADAPTER_ERR_SEND;

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

    return ADAPTER_OK;
}

static int ble_nina_recv(protocol_adapter_t *self, uint8_t *buf, size_t buf_len,
                         size_t *out_len, uint32_t timeout_ms)
{
    (void)self;
    if (!s_ble.initialized) return ADAPTER_ERR_RECV;

    uint32_t start = millis();
    while ((millis() - start) < timeout_ms) {
        BLE.poll();
        if (s_ble.rx_ready) {
            size_t to_copy = s_ble.rx_len < buf_len ? s_ble.rx_len : buf_len;
            memcpy(buf, s_ble.rx_buf, to_copy);
            *out_len = to_copy;
            s_ble.rx_ready = false;
            return ADAPTER_OK;
        }
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
        BLE.end();
        s_ble.initialized = false;
        s_ble.rx_ready = false;
        platform_log("ble: deinitialized");
    }
    return ADAPTER_OK;
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

/*
 * CPCBF — Arduino MKR WiFi 1010 Configuration
 * SAMD21 Cortex-M0+: 256 KB flash, 32 KB SRAM
 */
#ifndef CPCBF_ARDUINO_CONFIG_H
#define CPCBF_ARDUINO_CONFIG_H

/* Max payload set via build flag (-DBENCH_MAX_PAYLOAD=1432)
 * 1432 + 14 byte bench header = 1446, the WiFiNINA max UDP payload */

/* Maximum stored test results — ~12 KB with 24 bytes per entry.
 * Flood mode uses aggregate_only=1, so this only matters for RTT/RSSI. */
#ifndef MAX_RESULTS
#define MAX_RESULTS 500
#endif

/* Serial baud rate */
#define SERIAL_BAUD 115200

/* SoftAP defaults */
#define DEFAULT_AP_IP    "192.168.4.1"
#define DEFAULT_STA_IP   "192.168.4.2"
#define DEFAULT_ESSID    "CPCBF_MKR"
#define DEFAULT_CHANNEL  6
#define DEFAULT_PORT     5201

/* BLE GATT service and characteristic UUIDs */
#define BLE_SERVICE_UUID       "19B10000-E8F2-537E-4F6C-D104768A1214"
#define BLE_TX_CHAR_UUID       "19B10001-E8F2-537E-4F6C-D104768A1214"
#define BLE_RX_CHAR_UUID       "19B10002-E8F2-537E-4F6C-D104768A1214"
#define BLE_MAX_ATT_PAYLOAD    244
/* Ring + pool sizes are overridable per PlatformIO env so each per-test
 * firmware only compiles in the buffers it actually needs. Pool is used
 * only by flood; ring is used only by RTT/RSSI (central notifications). */
#ifndef BLE_RX_RING_SLOTS
#define BLE_RX_RING_SLOTS      16     /* small ring for ping-pong / RSSI */
#endif
#ifndef BLE_RX_POOL_BYTES
#define BLE_RX_POOL_BYTES      10000  /* ~10 KB linear pool for flood bursts.
                                        * 20B payloads → ~454 pkts
                                        * 127B payloads → ~70 pkts */
#endif
#define BLE_CONNECT_TIMEOUT_MS 60000
#define BLE_SCAN_ROUND_MS      3000   /* per-round scan timeout before restart */
#define BLE_SCAN_MAX_ROUNDS    20     /* 20 × 3s = 60s total scan budget
                                        * More restarts = more chances to catch
                                        * SCAN_RSP (required by ArduinoBLE) */
#define BLE_ADV_INTERVAL       32     /* 32 × 0.625ms = 20ms (fast advertising) */

#endif /* CPCBF_ARDUINO_CONFIG_H */

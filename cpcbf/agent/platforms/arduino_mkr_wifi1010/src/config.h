/*
 * CPCBF — Arduino MKR WiFi 1010 Configuration
 * SAMD21 Cortex-M0+: 256 KB flash, 32 KB SRAM
 */
#ifndef CPCBF_ARDUINO_CONFIG_H
#define CPCBF_ARDUINO_CONFIG_H

/* Max payload set via build flag (-DBENCH_MAX_PAYLOAD=1024) */

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

#endif /* CPCBF_ARDUINO_CONFIG_H */

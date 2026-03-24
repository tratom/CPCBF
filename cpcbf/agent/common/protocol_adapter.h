/*
 * CPCBF — Cross-Platform Communication Benchmarking Framework
 * Protocol Adapter Interface
 */
#ifndef CPCBF_PROTOCOL_ADAPTER_H
#define CPCBF_PROTOCOL_ADAPTER_H

#include <stdint.h>
#include <stddef.h>

/* Error codes */
#define ADAPTER_OK          0
#define ADAPTER_ERR_INIT   -1
#define ADAPTER_ERR_SEND   -2
#define ADAPTER_ERR_RECV   -3
#define ADAPTER_ERR_TIMEOUT -4
#define ADAPTER_ERR_RSSI   -5

/* Topology types */
typedef enum {
    TOPO_P2P = 0,       /* Wi-Fi Direct (default) */
    TOPO_ADHOC = 1,     /* Ad-hoc / IBSS */
    TOPO_BLE_L2CAP = 2, /* BLE L2CAP Connection-Oriented Channels */
    TOPO_BLE_GATT = 3,  /* BLE GATT characteristics (ArduinoBLE) */
} topology_t;

/* Role in the test */
typedef enum {
    ROLE_SENDER   = 0,
    ROLE_RECEIVER = 1,
} adapter_role_t;

/* Adapter configuration */
typedef struct {
    char iface_name[32];   /* e.g. "wlan0" */
    char peer_addr[48];    /* peer IP address for UDP */
    char peer_mac[24];     /* peer WiFi MAC for P2P discovery */
    uint16_t port;         /* UDP port */
    int channel;           /* radio channel */
    char essid[64];        /* SSID or network name */
    char local_ip[16];     /* local IP address */
    char netmask[16];      /* e.g. "255.255.255.0" */
    adapter_role_t role;
    topology_t topology;
    uint8_t ble_phy;       /* 1=1M, 2=2M PHY */
} adapter_config_t;

/* Forward declaration */
typedef struct protocol_adapter protocol_adapter_t;

/* Virtual function table */
struct protocol_adapter {
    int (*init)(protocol_adapter_t *self, const adapter_config_t *cfg);
    int (*send)(protocol_adapter_t *self, const uint8_t *data, size_t len);
    int (*recv)(protocol_adapter_t *self, uint8_t *buf, size_t buf_len,
                size_t *out_len, uint32_t timeout_ms);
    int (*get_rssi)(protocol_adapter_t *self, int *rssi_dbm);
    int (*deinit)(protocol_adapter_t *self);
    void *priv; /* adapter-specific private data */
};

#endif /* CPCBF_PROTOCOL_ADAPTER_H */

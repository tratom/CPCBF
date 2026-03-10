/*
 * CPCBF — BLE Setup for RPi4 (BCM43455)
 * Uses L2CAP Connection-Oriented Channels for data transfer.
 */
#ifndef CPCBF_BLE_SETUP_H
#define CPCBF_BLE_SETUP_H

#include "protocol_adapter.h"

/*
 * Set up a BLE L2CAP CoC connection for benchmarking.
 *
 * Role mapping:
 *   ROLE_RECEIVER = peripheral (advertises, accepts connection)
 *   ROLE_SENDER   = central (scans, initiates connection)
 *
 * cfg->peer_mac: peer BLE address (e.g. "DC:A6:32:81:07:A0")
 * cfg->port:     L2CAP PSM number (e.g. 0x80)
 * cfg->iface_name: HCI device name (e.g. "hci0")
 *
 * On success, writes the connected L2CAP socket fd to *l2cap_fd
 * and returns 0. On failure returns -1.
 */
int ble_setup(const adapter_config_t *cfg, int *l2cap_fd);

/*
 * Tear down the BLE connection and restore state.
 * Returns 0 on success, -1 on failure.
 */
int ble_teardown(const adapter_config_t *cfg);

#endif /* CPCBF_BLE_SETUP_H */

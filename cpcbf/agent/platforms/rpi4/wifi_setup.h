/*
 * CPCBF — Wi-Fi Setup for RPi4 (BCM43455)
 * Supports Wi-Fi Direct (P2P) and ad-hoc (IBSS) topologies.
 */
#ifndef CPCBF_WIFI_SETUP_H
#define CPCBF_WIFI_SETUP_H

#include "protocol_adapter.h"

/*
 * Set up the Wi-Fi interface for benchmarking.
 * Configures either Wi-Fi Direct or ad-hoc based on cfg->topology.
 *
 * On success, writes the interface name into iface_out (e.g. "p2p-wlan0-0")
 * and returns 0. On failure returns -1.
 */
int wifi_setup(const adapter_config_t *cfg, char *iface_out, size_t iface_out_len);

/*
 * Tear down the Wi-Fi interface and restore state.
 * Returns 0 on success, -1 on failure.
 */
int wifi_teardown(const adapter_config_t *cfg);

#endif /* CPCBF_WIFI_SETUP_H */

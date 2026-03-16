/*
 * CPCBF — BLE L2CAP CoC Setup for RPi4
 *
 * Uses BlueZ L2CAP Connection-Oriented Channels for data transfer.
 * Peripheral (receiver) advertises, central (sender) scans and connects.
 * Requires libbluetooth-dev and root privileges.
 */
#include "ble_setup.h"
#include "platform_hal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <bluetooth/bluetooth.h>
#include <bluetooth/l2cap.h>
#include <bluetooth/hci.h>
#include <bluetooth/hci_lib.h>

/* LE_LINK may not be defined in older BlueZ headers */
#ifndef LE_LINK
#define LE_LINK 0x80
#endif

/* BLE L2CAP CoC MTU — negotiated via socket options */
#define BLE_L2CAP_MTU 2048

/* Connection timeout for accept/connect */
#define BLE_CONNECT_TIMEOUT_S 60

/* Run a shell command with stdout redirected to stderr (keep stdout clean for JSON). */
static int run_cmd(const char *cmd)
{
    platform_log("exec: %s", cmd);
    char wrapped[512];
    snprintf(wrapped, sizeof(wrapped), "(%s) 1>&2", cmd);
    int rc = system(wrapped);
    if (rc != 0)
        platform_log("command failed (rc=%d): %s", rc, cmd);
    return rc;
}

/* Get the HCI device ID from name (e.g. "hci0" -> 0). */
static int get_hci_dev_id(const char *iface_name)
{
    if (strncmp(iface_name, "hci", 3) != 0)
        return 0;
    return atoi(iface_name + 3);
}

/* Reset and bring up the BLE adapter. */
static int ble_reset_adapter(const char *iface_name)
{
    /* Unblock Bluetooth radio — may have been soft-blocked by radio isolation
     * from a previous WiFi test or by rfkill during a failed run */
    run_cmd("rfkill unblock bluetooth 2>/dev/null");
    platform_sleep_ms(200);

    /* Stop bluetoothd temporarily to get raw HCI access if needed */
    run_cmd("systemctl stop bluetooth 2>/dev/null");
    platform_sleep_ms(500);

    /* Reset and bring up adapter */
    char cmd[128];
    snprintf(cmd, sizeof(cmd), "hciconfig %s reset 2>/dev/null", iface_name);
    run_cmd(cmd);
    platform_sleep_ms(500);

    snprintf(cmd, sizeof(cmd), "hciconfig %s up 2>/dev/null", iface_name);
    if (run_cmd(cmd) != 0) {
        /* Retry once after a longer pause — BCM43455 sometimes needs more
         * recovery time after reset */
        platform_sleep_ms(2000);
        run_cmd(cmd);
    }
    platform_sleep_ms(500);

    /* Enable Data Length Extension (DLE) — set default TX to max 251 bytes.
     * HCI LE Write Suggested Default Data Length (ogf=0x08, ocf=0x0024).
     * Without DLE the controller uses 27-byte PDUs, fragmenting every L2CAP
     * SDU into tiny chunks and capping throughput at ~130 Kbps.
     * With DLE (251B PDU) throughput can reach ~800 Kbps at 7.5ms CI. */
    snprintf(cmd, sizeof(cmd),
        "hcitool -i %s cmd 0x08 0x0024 FB 00 48 08 2>/dev/null", iface_name);
    run_cmd(cmd);

    /* Restart bluetoothd — needed for bluetoothctl commands */
    run_cmd("systemctl start bluetooth 2>/dev/null");
    platform_sleep_ms(1000);

    return 0;
}

/*
 * Request optimal link parameters on an active BLE connection.
 * - DLE per-connection: LE Set Data Length (ogf=0x08, ocf=0x0022)
 * - Minimum connection interval: 7.5ms via lecup
 */
static void ble_optimize_link(const char *iface_name, const char *peer_mac, uint8_t ble_phy)
{
    int dev_id = get_hci_dev_id(iface_name);
    int hci_fd = hci_open_dev(dev_id);
    if (hci_fd < 0) return;

    struct hci_conn_info_req *cr;
    cr = malloc(sizeof(*cr) + sizeof(struct hci_conn_info));
    if (!cr) { hci_close_dev(hci_fd); return; }

    str2ba(peer_mac, &cr->bdaddr);
    cr->type = LE_LINK;
    if (ioctl(hci_fd, HCIGETCONNINFO, cr) != 0) {
        platform_log("Could not get BLE conn handle for %s", peer_mac);
        free(cr);
        hci_close_dev(hci_fd);
        return;
    }
    uint16_t handle = cr->conn_info->handle;
    free(cr);

    char cmd[256];

    /* DLE per-connection: request 251-byte TX PDUs.
     * LE Set Data Length: handle, TxOctets=0x00FB(251), TxTime=0x0848(2120us) */
    snprintf(cmd, sizeof(cmd),
        "hcitool -i %s cmd 0x08 0x0022 %02x %02x FB 00 48 08 2>/dev/null",
        iface_name, handle & 0xFF, (handle >> 8) & 0xFF);
    run_cmd(cmd);

    /* Request minimum connection interval (7.5ms = 6 × 1.25ms) */
    snprintf(cmd, sizeof(cmd),
        "hcitool -i %s lecup --handle %d --min 6 --max 6 --latency 0 --timeout 200 2>/dev/null",
        iface_name, handle);
    run_cmd(cmd);

    /* Allow time for parameter negotiation */
    platform_sleep_ms(500);

    /* Switch to 2M PHY if requested.
     * LE Set PHY (ogf=0x08, ocf=0x0032):
     *   handle(2B), ALL_PHYS=0x00, TX_PHYS=0x02(2M), RX_PHYS=0x02(2M), PHY_Options=0x0000 */
    if (ble_phy == 2) {
        snprintf(cmd, sizeof(cmd),
            "hcitool -i %s cmd 0x08 0x0032 %02x %02x 00 02 02 00 00 2>/dev/null",
            iface_name, handle & 0xFF, (handle >> 8) & 0xFF);
        run_cmd(cmd);
        platform_sleep_ms(500);
        platform_log("BLE 2M PHY requested on handle=%d", handle);
    }

    platform_log("BLE link optimized: handle=%d, DLE=251B, CI=7.5ms, PHY=%s",
                 handle, ble_phy == 2 ? "2M" : "1M");
    hci_close_dev(hci_fd);
}

/* Set up as peripheral: advertise and accept L2CAP CoC connection. */
static int setup_peripheral(const adapter_config_t *cfg, int *l2cap_fd)
{
    platform_log("BLE peripheral setup on %s, PSM=%d", cfg->iface_name, cfg->port);

    ble_reset_adapter(cfg->iface_name);

    /* Power on and make discoverable */
    run_cmd("bluetoothctl power on");
    platform_sleep_ms(500);
    run_cmd("bluetoothctl discoverable on");
    run_cmd("bluetoothctl pairable on");
    platform_sleep_ms(500);

    /* Enable LE advertising */
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
        "hcitool -i %s cmd 0x08 0x0006 30 00 30 00 00 00 00 00 00 00 00 00 00 00 00 01 00",
        cfg->iface_name);
    run_cmd(cmd);  /* Set advertising parameters */

    snprintf(cmd, sizeof(cmd),
        "hcitool -i %s cmd 0x08 0x000A 01",
        cfg->iface_name);
    run_cmd(cmd);  /* Enable advertising */

    /* Create L2CAP server socket */
    int server_fd = socket(PF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP);
    if (server_fd < 0) {
        platform_log("L2CAP socket() failed: %s", strerror(errno));
        return -1;
    }

    /* Set BLE L2CAP CoC options */
    struct l2cap_options opts;
    memset(&opts, 0, sizeof(opts));
    socklen_t optlen = sizeof(opts);
    getsockopt(server_fd, SOL_L2CAP, L2CAP_OPTIONS, &opts, &optlen);
    opts.imtu = BLE_L2CAP_MTU;
    opts.omtu = BLE_L2CAP_MTU;
    setsockopt(server_fd, SOL_L2CAP, L2CAP_OPTIONS, &opts, sizeof(opts));

    /* Set security level to low (no pairing required for benchmarking) */
    struct bt_security sec;
    memset(&sec, 0, sizeof(sec));
    sec.level = BT_SECURITY_LOW;
    setsockopt(server_fd, SOL_BLUETOOTH, BT_SECURITY, &sec, sizeof(sec));

    /* Bind to local address */
    struct sockaddr_l2 local_addr;
    memset(&local_addr, 0, sizeof(local_addr));
    local_addr.l2_family = AF_BLUETOOTH;
    local_addr.l2_psm = htobs(cfg->port);
    local_addr.l2_bdaddr = *BDADDR_ANY;
    local_addr.l2_bdaddr_type = BDADDR_LE_PUBLIC;
    local_addr.l2_cid = 0;

    if (bind(server_fd, (struct sockaddr *)&local_addr, sizeof(local_addr)) < 0) {
        platform_log("L2CAP bind() failed: %s", strerror(errno));
        close(server_fd);
        return -1;
    }

    if (listen(server_fd, 1) < 0) {
        platform_log("L2CAP listen() failed: %s", strerror(errno));
        close(server_fd);
        return -1;
    }

    /* Set accept timeout */
    struct timeval tv;
    tv.tv_sec = BLE_CONNECT_TIMEOUT_S;
    tv.tv_usec = 0;
    setsockopt(server_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    platform_log("BLE peripheral listening on PSM %d, waiting for connection...", cfg->port);

    /* Accept connection */
    struct sockaddr_l2 remote_addr;
    socklen_t remote_len = sizeof(remote_addr);
    int client_fd = accept(server_fd, (struct sockaddr *)&remote_addr, &remote_len);

    close(server_fd);

    if (client_fd < 0) {
        platform_log("L2CAP accept() failed: %s", strerror(errno));
        return -1;
    }

    char peer_str[18];
    ba2str(&remote_addr.l2_bdaddr, peer_str);
    platform_log("BLE connection accepted from %s", peer_str);

    *l2cap_fd = client_fd;

    /* Optimize link: enable DLE + min connection interval + optional 2M PHY */
    ble_optimize_link(cfg->iface_name, peer_str, cfg->ble_phy);

    /* Wait for link stabilization */
    platform_log("Waiting 2s for BLE link stabilization...");
    platform_sleep_ms(2000);

    return 0;
}

/* Set up as central: scan for peer and connect via L2CAP CoC. */
static int setup_central(const adapter_config_t *cfg, int *l2cap_fd)
{
    platform_log("BLE central setup on %s, peer=%s, PSM=%d",
                 cfg->iface_name, cfg->peer_mac, cfg->port);

    ble_reset_adapter(cfg->iface_name);

    /* Power on */
    run_cmd("bluetoothctl power on");
    platform_sleep_ms(500);

    /* Scan for the peer device with retries */
    int found = 0;
    for (int attempt = 0; attempt < 6; attempt++) {
        platform_log("BLE scan attempt %d/6...", attempt + 1);

        /* Start LE scan — redirect ALL output to stderr to keep stdout clean */
        run_cmd("bluetoothctl --timeout 5 scan on >/dev/null 2>&1");

        /* Check if peer is visible */
        char check_cmd[128];
        snprintf(check_cmd, sizeof(check_cmd),
            "bluetoothctl devices 2>/dev/null | grep -qi '%s'", cfg->peer_mac);
        if (system(check_cmd) == 0) {
            platform_log("Found peer device %s", cfg->peer_mac);
            found = 1;
            break;
        }
        platform_sleep_ms(2000);
    }

    if (!found) {
        platform_log("Peer %s not found after scanning", cfg->peer_mac);
        /* Continue anyway — the peer may still be connectable */
    }

    /* Peer address — prepared once, reused across retries */
    struct sockaddr_l2 peer_addr;
    memset(&peer_addr, 0, sizeof(peer_addr));
    peer_addr.l2_family = AF_BLUETOOTH;
    peer_addr.l2_psm = htobs(cfg->port);
    peer_addr.l2_bdaddr_type = BDADDR_LE_PUBLIC;
    str2ba(cfg->peer_mac, &peer_addr.l2_bdaddr);

    platform_log("Connecting to %s on PSM %d...", cfg->peer_mac, cfg->port);

    /* Retry connect with fresh socket each time.
     * After a failed connect() the socket enters an error state (especially
     * ENOSYS / EINVAL from the BLE stack not being ready yet), so we must
     * close and recreate it before retrying. */
    int sock_fd = -1;
    int connected = 0;
    for (int attempt = 0; attempt < 10; attempt++) {
        /* Create a fresh L2CAP CoC socket */
        sock_fd = socket(PF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP);
        if (sock_fd < 0) {
            platform_log("L2CAP socket() failed: %s", strerror(errno));
            platform_sleep_ms(1000);
            continue;
        }

        /* Set BLE L2CAP CoC options */
        struct l2cap_options opts;
        memset(&opts, 0, sizeof(opts));
        socklen_t optlen = sizeof(opts);
        getsockopt(sock_fd, SOL_L2CAP, L2CAP_OPTIONS, &opts, &optlen);
        opts.imtu = BLE_L2CAP_MTU;
        opts.omtu = BLE_L2CAP_MTU;
        setsockopt(sock_fd, SOL_L2CAP, L2CAP_OPTIONS, &opts, sizeof(opts));

        /* Set security level */
        struct bt_security sec;
        memset(&sec, 0, sizeof(sec));
        sec.level = BT_SECURITY_LOW;
        setsockopt(sock_fd, SOL_BLUETOOTH, BT_SECURITY, &sec, sizeof(sec));

        /* Bind to local adapter */
        struct sockaddr_l2 local_addr;
        memset(&local_addr, 0, sizeof(local_addr));
        local_addr.l2_family = AF_BLUETOOTH;
        local_addr.l2_bdaddr = *BDADDR_ANY;
        local_addr.l2_bdaddr_type = BDADDR_LE_PUBLIC;

        if (bind(sock_fd, (struct sockaddr *)&local_addr, sizeof(local_addr)) < 0) {
            platform_log("L2CAP bind() failed: %s", strerror(errno));
            close(sock_fd);
            sock_fd = -1;
            platform_sleep_ms(1000);
            continue;
        }

        /* Set connect timeout */
        struct timeval tv;
        tv.tv_sec = BLE_CONNECT_TIMEOUT_S;
        tv.tv_usec = 0;
        setsockopt(sock_fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

        if (connect(sock_fd, (struct sockaddr *)&peer_addr, sizeof(peer_addr)) == 0) {
            connected = 1;
            break;
        }

        platform_log("L2CAP connect attempt %d failed: %s", attempt + 1, strerror(errno));
        close(sock_fd);
        sock_fd = -1;

        if (errno == EHOSTUNREACH || errno == ECONNREFUSED) {
            platform_sleep_ms(3000);
        } else {
            platform_sleep_ms(2000);
        }
    }

    if (!connected) {
        platform_log("L2CAP connect() to %s failed after retries", cfg->peer_mac);
        if (sock_fd >= 0) close(sock_fd);
        return -1;
    }

    platform_log("BLE L2CAP connected to %s", cfg->peer_mac);

    *l2cap_fd = sock_fd;

    /* Optimize link: enable DLE + min connection interval + optional 2M PHY */
    ble_optimize_link(cfg->iface_name, cfg->peer_mac, cfg->ble_phy);

    /* Wait for link stabilization */
    platform_log("Waiting 2s for BLE link stabilization...");
    platform_sleep_ms(2000);

    return 0;
}

/* ---- Public API ---- */

int ble_setup(const adapter_config_t *cfg, int *l2cap_fd)
{
    /* Ensure WiFi is disabled for radio isolation */
    platform_radio_disable("wifi");

    if (cfg->role == ROLE_RECEIVER)
        return setup_peripheral(cfg, l2cap_fd);
    else
        return setup_central(cfg, l2cap_fd);
}

int ble_teardown(const adapter_config_t *cfg)
{
    (void)cfg;

    /* Disable advertising */
    run_cmd("bluetoothctl discoverable off 2>/dev/null");
    run_cmd("bluetoothctl pairable off 2>/dev/null");

    /* Reset adapter to clean state */
    char cmd[128];
    snprintf(cmd, sizeof(cmd), "hciconfig %s reset 2>/dev/null",
             cfg->iface_name[0] ? cfg->iface_name : "hci0");
    run_cmd(cmd);

    platform_sleep_ms(500);
    return 0;
}

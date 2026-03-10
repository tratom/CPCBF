/*
 * CPCBF — BLE Protocol Adapter (L2CAP CoC sockets)
 *
 * Uses L2CAP Connection-Oriented Channels over BLE for data transfer.
 * SOCK_SEQPACKET preserves message boundaries — each send/recv maps to
 * one benchmark packet, matching the UDP datagram behavior of wifi_adapter.
 */
#include "protocol_adapter.h"
#include "platform_hal.h"
#include "ble_setup.h"

#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <bluetooth/bluetooth.h>
#include <bluetooth/l2cap.h>
#include <bluetooth/hci.h>
#include <bluetooth/hci_lib.h>

#ifndef LE_LINK
#define LE_LINK 0x80
#endif

typedef struct {
    int l2cap_fd;           /* connected L2CAP CoC socket */
    adapter_config_t cfg;
    uint16_t conn_handle;   /* HCI connection handle for RSSI queries */
    int hci_fd;             /* HCI device fd for RSSI queries */
} ble_priv_t;

/* Resolve the HCI connection handle for RSSI reads. */
static void resolve_conn_handle(ble_priv_t *priv)
{
    priv->conn_handle = 0;
    priv->hci_fd = -1;

    int dev_id = 0;
    if (strncmp(priv->cfg.iface_name, "hci", 3) == 0)
        dev_id = atoi(priv->cfg.iface_name + 3);

    int fd = hci_open_dev(dev_id);
    if (fd < 0) return;

    struct hci_conn_info_req *cr;
    cr = malloc(sizeof(*cr) + sizeof(struct hci_conn_info));
    if (!cr) {
        hci_close_dev(fd);
        return;
    }

    str2ba(priv->cfg.peer_mac, &cr->bdaddr);
    cr->type = LE_LINK;
    if (ioctl(fd, HCIGETCONNINFO, cr) == 0) {
        priv->conn_handle = cr->conn_info->handle;
        priv->hci_fd = fd;
        platform_log("BLE conn handle: %d", priv->conn_handle);
    } else {
        hci_close_dev(fd);
    }
    free(cr);
}

static int ble_init(protocol_adapter_t *self, const adapter_config_t *cfg)
{
    ble_priv_t *priv = calloc(1, sizeof(ble_priv_t));
    if (!priv)
        return ADAPTER_ERR_INIT;

    self->priv = priv;
    memcpy(&priv->cfg, cfg, sizeof(*cfg));
    priv->l2cap_fd = -1;
    priv->hci_fd = -1;

    /* Establish BLE L2CAP connection */
    if (ble_setup(cfg, &priv->l2cap_fd) != 0) {
        free(priv);
        self->priv = NULL;
        return ADAPTER_ERR_INIT;
    }

    /* Set default receive timeout */
    struct timeval tv;
    tv.tv_sec = 5;
    tv.tv_usec = 0;
    setsockopt(priv->l2cap_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    /* Resolve connection handle for RSSI queries */
    resolve_conn_handle(priv);

    platform_log("BLE adapter initialized: peer=%s PSM=%d",
                 cfg->peer_mac, cfg->port);
    return ADAPTER_OK;
}

static int ble_send(protocol_adapter_t *self, const uint8_t *data, size_t len)
{
    ble_priv_t *priv = self->priv;
    ssize_t sent = send(priv->l2cap_fd, data, len, 0);
    return (sent == (ssize_t)len) ? ADAPTER_OK : ADAPTER_ERR_SEND;
}

static int ble_recv(protocol_adapter_t *self, uint8_t *buf, size_t buf_len,
                    size_t *out_len, uint32_t timeout_ms)
{
    ble_priv_t *priv = self->priv;

    /* Set per-call receive timeout */
    struct timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(priv->l2cap_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    ssize_t n = recv(priv->l2cap_fd, buf, buf_len, 0);
    if (n < 0)
        return (errno == EAGAIN || errno == EWOULDBLOCK)
            ? ADAPTER_ERR_TIMEOUT : ADAPTER_ERR_RECV;
    if (n == 0)
        return ADAPTER_ERR_RECV; /* connection closed */
    if (out_len)
        *out_len = (size_t)n;
    return ADAPTER_OK;
}

static int ble_get_rssi(protocol_adapter_t *self, int *rssi_dbm)
{
    ble_priv_t *priv = self->priv;

    if (priv->hci_fd < 0 || priv->conn_handle == 0)
        return ADAPTER_ERR_RSSI;

    int8_t rssi;
    if (hci_read_rssi(priv->hci_fd, priv->conn_handle, &rssi, 1000) < 0)
        return ADAPTER_ERR_RSSI;

    *rssi_dbm = (int)rssi;
    return ADAPTER_OK;
}

static int ble_deinit(protocol_adapter_t *self)
{
    ble_priv_t *priv = self->priv;
    if (!priv)
        return ADAPTER_OK;

    if (priv->l2cap_fd >= 0)
        close(priv->l2cap_fd);
    if (priv->hci_fd >= 0)
        hci_close_dev(priv->hci_fd);

    ble_teardown(&priv->cfg);
    free(priv);
    self->priv = NULL;
    return ADAPTER_OK;
}

/* Public: create a BLE adapter instance */
protocol_adapter_t ble_adapter = {
    .init     = ble_init,
    .send     = ble_send,
    .recv     = ble_recv,
    .get_rssi = ble_get_rssi,
    .deinit   = ble_deinit,
    .priv     = NULL,
};

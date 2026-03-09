/*
 * CPCBF — WiFi Protocol Adapter (POSIX UDP sockets)
 */
#include "protocol_adapter.h"
#include "platform_hal.h"
#include "wifi_setup.h"
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdio.h>

typedef struct {
    int sock_fd;
    struct sockaddr_in peer_addr;
    adapter_config_t cfg;
    char active_iface[64];
} wifi_priv_t;

static int wifi_init(protocol_adapter_t *self, const adapter_config_t *cfg)
{
    wifi_priv_t *priv = calloc(1, sizeof(wifi_priv_t));
    if (!priv)
        return ADAPTER_ERR_INIT;

    self->priv = priv;
    memcpy(&priv->cfg, cfg, sizeof(*cfg));

    /* Set up Wi-Fi link */
    if (wifi_setup(cfg, priv->active_iface, sizeof(priv->active_iface)) != 0) {
        free(priv);
        self->priv = NULL;
        return ADAPTER_ERR_INIT;
    }

    /* Create UDP socket */
    priv->sock_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (priv->sock_fd < 0) {
        platform_log("socket() failed");
        wifi_teardown(cfg);
        free(priv);
        self->priv = NULL;
        return ADAPTER_ERR_INIT;
    }

    /* Bind to specific interface */
    if (setsockopt(priv->sock_fd, SOL_SOCKET, SO_BINDTODEVICE,
                   priv->active_iface, strlen(priv->active_iface) + 1) < 0) {
        platform_log("SO_BINDTODEVICE failed for %s", priv->active_iface);
        /* Non-fatal: continue without device binding */
    }

    /* Set receive timeout */
    struct timeval tv;
    tv.tv_sec = cfg->port > 0 ? 5 : 5; /* default 5s, refined per-recv */
    tv.tv_usec = 0;
    setsockopt(priv->sock_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    /* Bind to local address */
    struct sockaddr_in local;
    memset(&local, 0, sizeof(local));
    local.sin_family = AF_INET;
    local.sin_port = htons(cfg->port);
    local.sin_addr.s_addr = inet_addr(cfg->local_ip);

    if (bind(priv->sock_fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
        platform_log("bind() failed on %s:%d: %s", cfg->local_ip, cfg->port, strerror(errno));
        close(priv->sock_fd);
        wifi_teardown(cfg);
        free(priv);
        self->priv = NULL;
        return ADAPTER_ERR_INIT;
    }

    /* Set up peer address */
    memset(&priv->peer_addr, 0, sizeof(priv->peer_addr));
    priv->peer_addr.sin_family = AF_INET;
    priv->peer_addr.sin_port = htons(cfg->port);
    priv->peer_addr.sin_addr.s_addr = inet_addr(cfg->peer_addr);

    platform_log("WiFi adapter initialized: %s -> %s:%d",
                 priv->active_iface, cfg->peer_addr, cfg->port);
    return ADAPTER_OK;
}

static int wifi_send(protocol_adapter_t *self, const uint8_t *data, size_t len)
{
    wifi_priv_t *priv = self->priv;
    ssize_t sent = sendto(priv->sock_fd, data, len, 0,
                          (struct sockaddr *)&priv->peer_addr,
                          sizeof(priv->peer_addr));
    return (sent == (ssize_t)len) ? ADAPTER_OK : ADAPTER_ERR_SEND;
}

static int wifi_recv(protocol_adapter_t *self, uint8_t *buf, size_t buf_len,
                     size_t *out_len, uint32_t timeout_ms)
{
    wifi_priv_t *priv = self->priv;

    /* Set receive timeout for this call */
    struct timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(priv->sock_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    ssize_t n = recvfrom(priv->sock_fd, buf, buf_len, 0, NULL, NULL);
    if (n < 0)
        return ADAPTER_ERR_TIMEOUT;
    if (out_len)
        *out_len = (size_t)n;
    return ADAPTER_OK;
}

static int wifi_get_rssi(protocol_adapter_t *self, int *rssi_dbm)
{
    wifi_priv_t *priv = self->priv;

    /* Use iw station dump to get signal level — works for P2P interfaces */
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
        "wpa_cli -i %s signal_poll | grep 'RSSI=' | cut -d'=' -f2",
        priv->active_iface);

    FILE *fp = popen(cmd, "r");
    if (!fp)
        return ADAPTER_ERR_RSSI;

    char line[64];
    int found = 0;
    if (fgets(line, sizeof(line), fp)) {
        int val = atoi(line);
        if (val < 0) { /* RSSI is always negative dBm */
            *rssi_dbm = val;
            found = 1;
        }
    }
    pclose(fp);

    return found ? ADAPTER_OK : ADAPTER_ERR_RSSI;
}

static int wifi_deinit(protocol_adapter_t *self)
{
    wifi_priv_t *priv = self->priv;
    if (!priv)
        return ADAPTER_OK;

    if (priv->sock_fd >= 0)
        close(priv->sock_fd);
    wifi_teardown(&priv->cfg);
    free(priv);
    self->priv = NULL;
    return ADAPTER_OK;
}

/* Public: create a WiFi adapter instance */
protocol_adapter_t wifi_adapter = {
    .init     = wifi_init,
    .send     = wifi_send,
    .recv     = wifi_recv,
    .get_rssi = wifi_get_rssi,
    .deinit   = wifi_deinit,
    .priv     = NULL,
};

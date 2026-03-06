/*
 * CPCBF — Wi-Fi Setup Implementation for RPi4
 *
 * Assumes wpa_supplicant is managed by systemd (Bookworm+ default).
 * Uses wpa_cli for P2P operations against the running instance.
 */
#include "wifi_setup.h"
#include "platform_hal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

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

/* Run a command and capture first line of output. Returns 0 on success. */
static int run_cmd_output(const char *cmd, char *out, size_t out_len)
{
    FILE *fp = popen(cmd, "r");
    if (!fp) return -1;
    if (!fgets(out, (int)out_len, fp)) {
        pclose(fp);
        return -1;
    }
    pclose(fp);
    /* Strip trailing newline */
    size_t len = strlen(out);
    if (len > 0 && out[len - 1] == '\n')
        out[len - 1] = '\0';
    return 0;
}

/* Wait for an interface to appear (up to timeout_s seconds). */
static int wait_for_iface(const char *prefix, int timeout_s,
                          char *iface_out, size_t iface_out_len)
{
    char cmd[256];
    for (int i = 0; i < timeout_s * 2; i++) {
        snprintf(cmd, sizeof(cmd),
            "ls /sys/class/net/ | grep '^%s' | head -1", prefix);
        if (run_cmd_output(cmd, iface_out, iface_out_len) == 0 &&
            strlen(iface_out) > 0)
            return 0;
        usleep(500000);
    }
    return -1;
}

/* Wait until wpa_cli can connect to wlan0 (up to timeout_s seconds). */
static int wait_for_wpa_cli(int timeout_s)
{
    for (int i = 0; i < timeout_s * 2; i++) {
        int rc = system("wpa_cli -i wlan0 status >/dev/null 2>&1");
        if (rc == 0) {
            platform_log("wpa_cli ready after %d ms", i * 500);
            return 0;
        }
        usleep(500000);
    }
    return -1;
}

/* Clean up any existing P2P groups and restart wpa_supplicant for a clean state. */
static void cleanup_p2p(void)
{
    /* Try to remove any existing P2P groups */
    run_cmd("wpa_cli -i wlan0 p2p_group_remove \"*\" 2>/dev/null");
    /* Restart wpa_supplicant via systemd for a clean state */
    run_cmd("systemctl restart wpa_supplicant 2>/dev/null");
    /* Wait until wpa_supplicant control interface is ready */
    if (wait_for_wpa_cli(10) != 0)
        platform_log("WARNING: wpa_cli not ready after 10s");
}

/* ---- Wi-Fi Direct (P2P) ---- */

static int setup_p2p(const adapter_config_t *cfg,
                     char *iface_out, size_t iface_out_len)
{
    cleanup_p2p();

    if (cfg->role == ROLE_SENDER) {
        /* GO role: create P2P group */
        char ch_cmd[128];
        snprintf(ch_cmd, sizeof(ch_cmd),
            "wpa_cli -i wlan0 p2p_group_add freq=%d",
            cfg->channel > 0 ? cfg->channel : 2437);
        if (run_cmd(ch_cmd) != 0) {
            platform_log("p2p_group_add failed");
            return -1;
        }
        platform_sleep_ms(2000);

        if (wait_for_iface("p2p-wlan0", 10, iface_out, iface_out_len) != 0) {
            platform_log("P2P group interface did not appear");
            return -1;
        }

        /* Assign IP (CIDR notation) */
        char ip_cmd[128];
        snprintf(ip_cmd, sizeof(ip_cmd),
            "ip addr add %s/24 dev %s 2>/dev/null; ip link set %s up",
            cfg->local_ip, iface_out, iface_out);
        run_cmd(ip_cmd);

        /* Enable WPS push button for client connection */
        char wps_cmd[128];
        snprintf(wps_cmd, sizeof(wps_cmd),
            "wpa_cli -i %s wps_pbc", iface_out);
        run_cmd(wps_cmd);  /* non-fatal if it fails */

    } else {
        /* Client role: find and connect to GO */
        run_cmd("wpa_cli -i wlan0 p2p_find");
        platform_sleep_ms(5000);  /* give time to discover the GO */

        char connect_cmd[256];
        snprintf(connect_cmd, sizeof(connect_cmd),
            "wpa_cli -i wlan0 p2p_connect %s pbc join", cfg->peer_mac);
        if (run_cmd(connect_cmd) != 0) {
            platform_log("p2p_connect failed");
            return -1;
        }

        if (wait_for_iface("p2p-wlan0", 20, iface_out, iface_out_len) != 0) {
            platform_log("P2P client interface did not appear");
            return -1;
        }

        char ip_cmd[128];
        snprintf(ip_cmd, sizeof(ip_cmd),
            "ip addr add %s/24 dev %s 2>/dev/null; ip link set %s up",
            cfg->local_ip, iface_out, iface_out);
        run_cmd(ip_cmd);
    }

    platform_log("P2P interface ready: %s", iface_out);
    return 0;
}

/* ---- Ad-hoc (IBSS) ---- */

static int setup_adhoc(const adapter_config_t *cfg,
                       char *iface_out, size_t iface_out_len)
{
    /* For ad-hoc, we do need to stop wpa_supplicant to change interface type */
    run_cmd("systemctl stop wpa_supplicant 2>/dev/null");
    run_cmd("killall dhcpcd 2>/dev/null");
    platform_sleep_ms(500);

    run_cmd("ip link set wlan0 down");
    run_cmd("iw dev wlan0 set type ibss");
    run_cmd("ip link set wlan0 up");

    char join_cmd[256];
    snprintf(join_cmd, sizeof(join_cmd),
        "iw dev wlan0 ibss join %s %d",
        strlen(cfg->essid) > 0 ? cfg->essid : "CPCBF_TEST",
        cfg->channel > 0 ? cfg->channel : 2437);
    if (run_cmd(join_cmd) != 0) {
        platform_log("ibss join failed");
        return -1;
    }

    char ip_cmd[128];
    snprintf(ip_cmd, sizeof(ip_cmd),
        "ip addr add %s/24 dev wlan0 2>/dev/null",
        cfg->local_ip);
    run_cmd(ip_cmd);

    snprintf(iface_out, iface_out_len, "wlan0");
    platform_log("Ad-hoc interface ready: wlan0");
    return 0;
}

/* ---- Public API ---- */

int wifi_setup(const adapter_config_t *cfg, char *iface_out, size_t iface_out_len)
{
    if (cfg->topology == TOPO_ADHOC)
        return setup_adhoc(cfg, iface_out, iface_out_len);
    else
        return setup_p2p(cfg, iface_out, iface_out_len);
}

int wifi_teardown(const adapter_config_t *cfg)
{
    (void)cfg;
    /* Remove all P2P groups */
    run_cmd("wpa_cli -i wlan0 p2p_group_remove \"*\" 2>/dev/null");
    /* Restart wpa_supplicant to restore clean state */
    run_cmd("systemctl restart wpa_supplicant 2>/dev/null");
    return 0;
}

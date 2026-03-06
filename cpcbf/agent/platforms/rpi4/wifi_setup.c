/*
 * CPCBF — Wi-Fi Direct Setup for RPi4
 *
 * Based on tested working scripts from Iot-Experiment-Runner.
 * Stops NetworkManager, starts own wpa_supplicant, uses P2P.
 */
#include "wifi_setup.h"
#include "platform_hal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>

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
    size_t len = strlen(out);
    if (len > 0 && out[len - 1] == '\n')
        out[len - 1] = '\0';
    return 0;
}

/* Wait for p2p-wlan0-* interface to appear. */
static int wait_for_iface(int timeout_s, char *iface_out, size_t iface_out_len)
{
    for (int i = 0; i < timeout_s; i++) {
        if (run_cmd_output(
                "ls /sys/class/net/ | grep '^p2p-wlan0' | head -1",
                iface_out, iface_out_len) == 0 &&
            strlen(iface_out) > 0) {
            platform_log("P2P interface appeared: %s (after %ds)", iface_out, i);
            return 0;
        }
        sleep(1);
    }
    return -1;
}

/* Ensure minimal wpa_supplicant.conf exists. */
static void ensure_wpa_conf(void)
{
    struct stat st;
    if (stat("/etc/wpa_supplicant/wpa_supplicant.conf", &st) == 0)
        return; /* already exists */

    run_cmd("mkdir -p /etc/wpa_supplicant");
    FILE *fp = fopen("/etc/wpa_supplicant/wpa_supplicant.conf", "w");
    if (fp) {
        fprintf(fp,
            "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
            "update_config=1\n"
            "device_name=CPCBF\n"
            "device_type=1-0050F204-1\n"
            "p2p_go_intent=15\n");
        fclose(fp);
        platform_log("Created minimal wpa_supplicant.conf");
    }
}

/* Clean up: kill wpa_supplicant, stop NetworkManager */
static void full_cleanup(void)
{
    run_cmd("pkill -9 wpa_supplicant 2>/dev/null");
    sleep(2);
    run_cmd("systemctl stop NetworkManager 2>/dev/null");
    run_cmd("rfkill unblock wifi 2>/dev/null");
    run_cmd("ip link set wlan0 up 2>/dev/null");
    sleep(1);
}

/* Start our own wpa_supplicant and wait for it. */
static int start_wpa_supplicant(void)
{
    ensure_wpa_conf();
    run_cmd("wpa_supplicant -B -i wlan0 "
            "-c /etc/wpa_supplicant/wpa_supplicant.conf");
    sleep(3);

    /* Verify it's running */
    int rc = system("wpa_cli -i wlan0 status >/dev/null 2>&1");
    if (rc != 0) {
        platform_log("wpa_supplicant failed to start");
        return -1;
    }
    platform_log("wpa_supplicant ready");
    return 0;
}

/* ---- Wi-Fi Direct (P2P) — GO role ---- */

static int setup_p2p_go(const adapter_config_t *cfg,
                        char *iface_out, size_t iface_out_len)
{
    full_cleanup();
    if (start_wpa_supplicant() != 0)
        return -1;

    /* Create P2P group (become GO) */
    run_cmd("wpa_cli -i wlan0 p2p_group_add");

    /* Wait for p2p-wlan0-0 interface */
    if (wait_for_iface(20, iface_out, iface_out_len) != 0) {
        platform_log("P2P group interface did not appear");
        return -1;
    }

    /* Assign IP */
    char ip_cmd[256];
    snprintf(ip_cmd, sizeof(ip_cmd),
        "ip addr add %s/24 dev %s; ip link set %s up",
        cfg->local_ip, iface_out, iface_out);
    run_cmd(ip_cmd);

    /* Open WPS push button for client */
    char wps_cmd[128];
    snprintf(wps_cmd, sizeof(wps_cmd), "wpa_cli -i %s wps_pbc", iface_out);
    run_cmd(wps_cmd);

    platform_log("GO ready on %s with IP %s", iface_out, cfg->local_ip);
    return 0;
}

/* ---- Wi-Fi Direct (P2P) — Client role ---- */

static int setup_p2p_client(const adapter_config_t *cfg,
                            char *iface_out, size_t iface_out_len)
{
    full_cleanup();
    if (start_wpa_supplicant() != 0)
        return -1;

    /* Discover peers */
    run_cmd("wpa_cli -i wlan0 p2p_flush");
    run_cmd("wpa_cli -i wlan0 p2p_find");
    platform_log("Scanning for GO (10s)...");
    sleep(10);

    /* Connect to GO using peer MAC */
    char connect_cmd[256];
    snprintf(connect_cmd, sizeof(connect_cmd),
        "wpa_cli -i wlan0 p2p_connect %s pbc join", cfg->peer_mac);
    run_cmd(connect_cmd);

    /* Wait for p2p-wlan0-0 interface (up to 60s, retry at 20s) */
    int found = 0;
    for (int i = 0; i < 60; i++) {
        if (run_cmd_output(
                "ls /sys/class/net/ | grep '^p2p-wlan0' | head -1",
                iface_out, iface_out_len) == 0 &&
            strlen(iface_out) > 0) {
            platform_log("P2P client interface appeared: %s (after %ds)", iface_out, i);
            found = 1;
            break;
        }
        /* Retry connection at the 20s mark */
        if (i == 20) {
            platform_log("Retrying p2p_connect...");
            run_cmd(connect_cmd);
        }
        sleep(1);
    }

    if (!found) {
        platform_log("P2P client interface did not appear after 60s");
        return -1;
    }

    /* Assign IP */
    char ip_cmd[256];
    snprintf(ip_cmd, sizeof(ip_cmd),
        "ip addr add %s/24 dev %s; ip link set %s up",
        cfg->local_ip, iface_out, iface_out);
    run_cmd(ip_cmd);

    /* Wait for link stabilization */
    platform_log("Waiting 5s for link stabilization...");
    sleep(5);

    platform_log("Client ready on %s with IP %s", iface_out, cfg->local_ip);
    return 0;
}

/* ---- Ad-hoc (IBSS) ---- */

static int setup_adhoc(const adapter_config_t *cfg,
                       char *iface_out, size_t iface_out_len)
{
    full_cleanup();

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
        "ip addr add %s/24 dev wlan0 2>/dev/null", cfg->local_ip);
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

    if (cfg->role == ROLE_SENDER)
        return setup_p2p_go(cfg, iface_out, iface_out_len);
    else
        return setup_p2p_client(cfg, iface_out, iface_out_len);
}

int wifi_teardown(const adapter_config_t *cfg)
{
    (void)cfg;
    run_cmd("pkill -9 wpa_supplicant 2>/dev/null");
    sleep(2);
    run_cmd("systemctl start NetworkManager 2>/dev/null");
    return 0;
}

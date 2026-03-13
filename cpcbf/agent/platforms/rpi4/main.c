/*
 * CPCBF — RPi4 Agent Main
 * JSON command-response loop over stdin/stdout.
 * All logging goes to stderr; structured responses go to stdout.
 */
#include "protocol_adapter.h"
#include "platform_hal.h"
#include "benchmark_packet.h"
#include "test_engine.h"
#include "sync_barrier.h"
#include "cJSON.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/*
 * Protected JSON output stream.
 *
 * We dup the real stdout to a private fd, then redirect fd 1 → stderr.
 * This way, any subprocess (system(), popen(), bluetoothctl, hcitool, etc.)
 * inherits fd 1 = stderr, so their output can never pollute the JSON channel.
 * Only send_response() writes to g_json_out.
 */
static FILE *g_json_out = NULL;

/* External adapters */
extern protocol_adapter_t wifi_adapter;
extern protocol_adapter_t ble_adapter;

/* Agent state */
static adapter_config_t g_adapter_cfg;
static test_config_t g_test_cfg;
static test_results_t *g_results = NULL;
static protocol_adapter_t *g_active_adapter = NULL;
static int g_configured = 0;
static int g_adapter_ready = 0;

static void send_response(const char *status, const char *message, cJSON *data)
{
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "status", status);
    if (message)
        cJSON_AddStringToObject(resp, "message", message);
    if (data)
        cJSON_AddItemToObject(resp, "data", data);

    char *json = cJSON_PrintUnformatted(resp);
    fprintf(g_json_out, "%s\n", json);
    fflush(g_json_out);
    free(json);
    cJSON_Delete(resp);
}

static void send_ok(const char *message, cJSON *data)
{
    send_response("ok", message, data);
}

static void send_error(const char *message)
{
    send_response("error", message, NULL);
}

static void handle_configure(cJSON *params)
{
    cJSON *j;

    /* Adapter config */
    memset(&g_adapter_cfg, 0, sizeof(g_adapter_cfg));
    j = cJSON_GetObjectItem(params, "iface_name");
    if (j) strncpy(g_adapter_cfg.iface_name, j->valuestring, sizeof(g_adapter_cfg.iface_name) - 1);
    j = cJSON_GetObjectItem(params, "peer_addr");
    if (j) strncpy(g_adapter_cfg.peer_addr, j->valuestring, sizeof(g_adapter_cfg.peer_addr) - 1);
    j = cJSON_GetObjectItem(params, "peer_mac");
    if (j) strncpy(g_adapter_cfg.peer_mac, j->valuestring, sizeof(g_adapter_cfg.peer_mac) - 1);
    j = cJSON_GetObjectItem(params, "port");
    if (j) g_adapter_cfg.port = (uint16_t)j->valueint;
    j = cJSON_GetObjectItem(params, "channel");
    if (j) g_adapter_cfg.channel = j->valueint;
    j = cJSON_GetObjectItem(params, "essid");
    if (j) strncpy(g_adapter_cfg.essid, j->valuestring, sizeof(g_adapter_cfg.essid) - 1);
    j = cJSON_GetObjectItem(params, "local_ip");
    if (j) strncpy(g_adapter_cfg.local_ip, j->valuestring, sizeof(g_adapter_cfg.local_ip) - 1);
    j = cJSON_GetObjectItem(params, "netmask");
    if (j) strncpy(g_adapter_cfg.netmask, j->valuestring, sizeof(g_adapter_cfg.netmask) - 1);

    j = cJSON_GetObjectItem(params, "role");
    if (j) {
        if (strcmp(j->valuestring, "sender") == 0)
            g_adapter_cfg.role = ROLE_SENDER;
        else
            g_adapter_cfg.role = ROLE_RECEIVER;
    }

    j = cJSON_GetObjectItem(params, "topology");
    if (j && strcmp(j->valuestring, "adhoc") == 0)
        g_adapter_cfg.topology = TOPO_ADHOC;
    else if (j && strcmp(j->valuestring, "ble_l2cap") == 0)
        g_adapter_cfg.topology = TOPO_BLE_L2CAP;
    else
        g_adapter_cfg.topology = TOPO_P2P;

    j = cJSON_GetObjectItem(params, "ble_phy");
    if (j) g_adapter_cfg.ble_phy = (uint8_t)j->valueint;
    else   g_adapter_cfg.ble_phy = 1;

    /* Select adapter based on protocol */
    j = cJSON_GetObjectItem(params, "protocol");
    if (j && strcmp(j->valuestring, "ble") == 0)
        g_active_adapter = &ble_adapter;
    else
        g_active_adapter = &wifi_adapter;

    /* Test config */
    memset(&g_test_cfg, 0, sizeof(g_test_cfg));
    g_test_cfg.role = g_adapter_cfg.role;

    j = cJSON_GetObjectItem(params, "mode");
    if (j && strcmp(j->valuestring, "flood") == 0)
        g_test_cfg.mode = TEST_MODE_FLOOD;
    else if (j && strcmp(j->valuestring, "rssi") == 0)
        g_test_cfg.mode = TEST_MODE_RSSI;
    else
        g_test_cfg.mode = TEST_MODE_PING_PONG;

    j = cJSON_GetObjectItem(params, "payload_size");
    if (j) g_test_cfg.payload_size = (uint16_t)j->valueint;
    else g_test_cfg.payload_size = 128;

    j = cJSON_GetObjectItem(params, "repetitions");
    if (j) g_test_cfg.repetitions = (uint32_t)j->valueint;
    else g_test_cfg.repetitions = 100;

    j = cJSON_GetObjectItem(params, "warmup");
    if (j) g_test_cfg.warmup = (uint32_t)j->valueint;
    else g_test_cfg.warmup = 5;

    j = cJSON_GetObjectItem(params, "timeout_ms");
    if (j) g_test_cfg.timeout_ms = (uint32_t)j->valueint;
    else g_test_cfg.timeout_ms = 5000;

    j = cJSON_GetObjectItem(params, "inter_packet_us");
    if (j) g_test_cfg.inter_packet_us = (uint32_t)j->valueint;
    else g_test_cfg.inter_packet_us = 0;

    g_configured = 1;
    send_ok("configured", NULL);
}

static void handle_radio_disable(cJSON *params)
{
    const char *subsystem = "wifi";
    cJSON *j = cJSON_GetObjectItem(params, "subsystem");
    if (j) subsystem = j->valuestring;

    if (platform_radio_disable(subsystem) == 0)
        send_ok("radio disabled", NULL);
    else
        send_error("failed to disable radio");
}

static void handle_radio_status(void)
{
    cJSON *data = cJSON_CreateObject();

    int wifi_active = platform_radio_is_active("wifi");
    int bt_active = platform_radio_is_active("bluetooth");
    cJSON_AddBoolToObject(data, "wifi_active", wifi_active > 0);
    cJSON_AddBoolToObject(data, "bluetooth_active", bt_active > 0);

    send_ok("radio status", data);
}

static void handle_link_setup(const char *label)
{
    if (!g_configured || !g_active_adapter) {
        send_error("not configured");
        return;
    }

    /* Tear down any previous adapter state */
    if (g_adapter_ready) {
        g_active_adapter->deinit(g_active_adapter);
        g_adapter_ready = 0;
    }

    int rc = g_active_adapter->init(g_active_adapter, &g_adapter_cfg);
    if (rc != ADAPTER_OK) {
        send_error("adapter init failed");
        return;
    }

    g_adapter_ready = 1;
    char msg[64];
    snprintf(msg, sizeof(msg), "%s setup complete", label);
    send_ok(msg, NULL);
}

static void handle_sync(cJSON *params)
{
    if (!g_adapter_ready || !g_active_adapter) {
        send_error("adapter not ready — run WIFI_SETUP/BLE_SETUP first");
        return;
    }

    uint32_t timeout_ms = 120000;
    cJSON *j = cJSON_GetObjectItem(params, "timeout_ms");
    if (j) timeout_ms = (uint32_t)j->valueint;

    int rc = sync_barrier_wait(g_active_adapter, timeout_ms);
    if (rc != 0) {
        send_error("sync timeout — peer not reachable");
        return;
    }

    send_ok("sync complete", NULL);
}

static void handle_start(void)
{
    if (!g_configured) {
        send_error("not configured");
        return;
    }

    /* Free previous results */
    if (g_results) {
        test_results_free(g_results);
        g_results = NULL;
    }

    /* Initialize adapter if not already done via WIFI_SETUP/BLE_SETUP */
    if (!g_adapter_ready) {
        if (!g_active_adapter) g_active_adapter = &wifi_adapter;
        int rc = g_active_adapter->init(g_active_adapter, &g_adapter_cfg);
        if (rc != ADAPTER_OK) {
            send_error("adapter init failed");
            return;
        }
        g_adapter_ready = 1;
    }

    platform_log("starting test: mode=%d role=%d payload=%u reps=%u",
                 g_test_cfg.mode, g_test_cfg.role,
                 g_test_cfg.payload_size, g_test_cfg.repetitions);

    /* Run test (blocking) */
    g_results = test_engine_run(g_active_adapter, &g_test_cfg);

    if (g_results)
        send_ok("test complete", NULL);
    else
        send_error("test engine failed");
}

static void handle_get_results(void)
{
    if (!g_results) {
        send_error("no results available");
        return;
    }

    char *json_str = test_results_to_json(g_results, &g_test_cfg);
    if (!json_str) {
        send_error("failed to serialize results");
        return;
    }

    cJSON *data = cJSON_Parse(json_str);
    free(json_str);

    if (!data) {
        send_error("failed to parse results JSON");
        return;
    }

    send_ok("results", data);
}

static void handle_stop(void)
{
    if (g_results) {
        test_results_free(g_results);
        g_results = NULL;
    }
    if (g_adapter_ready && g_active_adapter) {
        g_active_adapter->deinit(g_active_adapter);
        g_adapter_ready = 0;
    }
    g_configured = 0;
    send_ok("stopped", NULL);
}

int main(void)
{
    /* Protect stdout: save real stdout, then redirect fd 1 → stderr
     * so that no subprocess can pollute the JSON channel. */
    int json_fd = dup(STDOUT_FILENO);
    dup2(STDERR_FILENO, STDOUT_FILENO);
    g_json_out = fdopen(json_fd, "w");

    platform_log("CPCBF agent started on %s", platform_name());

    char line[65536];
    while (fgets(line, sizeof(line), stdin)) {
        /* Strip trailing whitespace */
        size_t len = strlen(line);
        while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r'))
            line[--len] = '\0';
        if (len == 0) continue;

        cJSON *cmd = cJSON_Parse(line);
        if (!cmd) {
            send_error("invalid JSON");
            continue;
        }

        cJSON *j_cmd = cJSON_GetObjectItem(cmd, "command");
        if (!j_cmd || !cJSON_IsString(j_cmd)) {
            send_error("missing 'command' field");
            cJSON_Delete(cmd);
            continue;
        }

        const char *command = j_cmd->valuestring;
        cJSON *params = cJSON_GetObjectItem(cmd, "params");

        if (strcmp(command, "CONFIGURE") == 0) {
            handle_configure(params ? params : cJSON_CreateObject());
        } else if (strcmp(command, "RADIO_DISABLE") == 0) {
            handle_radio_disable(params ? params : cJSON_CreateObject());
        } else if (strcmp(command, "RADIO_STATUS") == 0) {
            handle_radio_status();
        } else if (strcmp(command, "WIFI_SETUP") == 0) {
            handle_link_setup("wifi");
        } else if (strcmp(command, "BLE_SETUP") == 0) {
            handle_link_setup("ble");
        } else if (strcmp(command, "SYNC") == 0) {
            handle_sync(params ? params : cJSON_CreateObject());
        } else if (strcmp(command, "START") == 0) {
            handle_start();
        } else if (strcmp(command, "GET_RESULTS") == 0) {
            handle_get_results();
        } else if (strcmp(command, "STOP") == 0) {
            handle_stop();
        } else {
            send_error("unknown command");
        }

        cJSON_Delete(cmd);
    }

    /* Cleanup */
    if (g_results)
        test_results_free(g_results);
    if (g_active_adapter)
        g_active_adapter->deinit(g_active_adapter);

    platform_log("agent exiting");
    return 0;
}

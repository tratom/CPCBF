/*
 * CPCBF — RPi4 Agent Main (C++ / ArduinoJson)
 * JSON command-response loop over stdin/stdout.
 * All logging goes to stderr; structured responses go to stdout.
 */
extern "C" {
#include "protocol_adapter.h"
#include "platform_hal.h"
#include "benchmark_packet.h"
#include "test_engine.h"
#include "sync_barrier.h"
}

#include <ArduinoJson.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
#include <string>

/*
 * Protected JSON output stream.
 *
 * We dup the real stdout to a private fd, then redirect fd 1 → stderr.
 * This way, any subprocess (system(), popen(), bluetoothctl, hcitool, etc.)
 * inherits fd 1 = stderr, so their output can never pollute the JSON channel.
 * Only send_response() writes to g_json_out.
 */
static FILE *g_json_out = nullptr;

/* External adapters */
extern "C" {
extern protocol_adapter_t wifi_adapter;
extern protocol_adapter_t ble_adapter;
}

/* Agent state */
static adapter_config_t g_adapter_cfg;
static test_config_t g_test_cfg;
static test_results_t *g_results = nullptr;
static protocol_adapter_t *g_active_adapter = nullptr;
static int g_configured = 0;
static int g_adapter_ready = 0;

static void send_json(JsonDocument &doc)
{
    std::string out;
    serializeJson(doc, out);
    fprintf(g_json_out, "%s\n", out.c_str());
    fflush(g_json_out);
}

static void send_response(const char *status, const char *message, JsonObject *data)
{
    JsonDocument doc;
    doc["status"] = status;
    if (message)
        doc["message"] = message;
    if (data)
        doc["data"] = *data;
    send_json(doc);
}

static void send_ok(const char *message)
{
    send_response("ok", message, nullptr);
}

static void send_ok_data(const char *message, JsonObject data)
{
    send_response("ok", message, &data);
}

static void send_error(const char *message)
{
    send_response("error", message, nullptr);
}

static void handle_configure(JsonObject params)
{
    /* Adapter config */
    memset(&g_adapter_cfg, 0, sizeof(g_adapter_cfg));

    if (params["iface_name"].is<const char *>())
        strncpy(g_adapter_cfg.iface_name, params["iface_name"].as<const char *>(), sizeof(g_adapter_cfg.iface_name) - 1);
    if (params["peer_addr"].is<const char *>())
        strncpy(g_adapter_cfg.peer_addr, params["peer_addr"].as<const char *>(), sizeof(g_adapter_cfg.peer_addr) - 1);
    if (params["peer_mac"].is<const char *>())
        strncpy(g_adapter_cfg.peer_mac, params["peer_mac"].as<const char *>(), sizeof(g_adapter_cfg.peer_mac) - 1);
    if (params["port"].is<int>())
        g_adapter_cfg.port = (uint16_t)params["port"].as<int>();
    if (params["channel"].is<int>())
        g_adapter_cfg.channel = params["channel"].as<int>();
    if (params["essid"].is<const char *>())
        strncpy(g_adapter_cfg.essid, params["essid"].as<const char *>(), sizeof(g_adapter_cfg.essid) - 1);
    if (params["local_ip"].is<const char *>())
        strncpy(g_adapter_cfg.local_ip, params["local_ip"].as<const char *>(), sizeof(g_adapter_cfg.local_ip) - 1);
    if (params["netmask"].is<const char *>())
        strncpy(g_adapter_cfg.netmask, params["netmask"].as<const char *>(), sizeof(g_adapter_cfg.netmask) - 1);

    if (params["role"].is<const char *>()) {
        if (strcmp(params["role"].as<const char *>(), "sender") == 0)
            g_adapter_cfg.role = ROLE_SENDER;
        else
            g_adapter_cfg.role = ROLE_RECEIVER;
    }

    if (params["topology"].is<const char *>()) {
        const char *topo = params["topology"].as<const char *>();
        if (strcmp(topo, "adhoc") == 0)
            g_adapter_cfg.topology = TOPO_ADHOC;
        else if (strcmp(topo, "ble_l2cap") == 0)
            g_adapter_cfg.topology = TOPO_BLE_L2CAP;
        else if (strcmp(topo, "softap") == 0)
            g_adapter_cfg.topology = TOPO_P2P;  /* softap maps to P2P on RPi4 */
        else
            g_adapter_cfg.topology = TOPO_P2P;
    }

    if (params["ble_phy"].is<int>())
        g_adapter_cfg.ble_phy = (uint8_t)params["ble_phy"].as<int>();
    else
        g_adapter_cfg.ble_phy = 1;

    /* Select adapter based on protocol */
    if (params["protocol"].is<const char *>() && strcmp(params["protocol"].as<const char *>(), "ble") == 0)
        g_active_adapter = &ble_adapter;
    else
        g_active_adapter = &wifi_adapter;

    /* Test config */
    memset(&g_test_cfg, 0, sizeof(g_test_cfg));
    g_test_cfg.role = g_adapter_cfg.role;

    if (params["mode"].is<const char *>()) {
        const char *mode = params["mode"].as<const char *>();
        if (strcmp(mode, "flood") == 0)
            g_test_cfg.mode = TEST_MODE_FLOOD;
        else if (strcmp(mode, "rssi") == 0)
            g_test_cfg.mode = TEST_MODE_RSSI;
        else
            g_test_cfg.mode = TEST_MODE_PING_PONG;
    }

    g_test_cfg.payload_size = params["payload_size"] | (uint16_t)128;
    g_test_cfg.repetitions = params["repetitions"] | (uint32_t)100;
    g_test_cfg.warmup = params["warmup"] | (uint32_t)5;
    g_test_cfg.timeout_ms = params["timeout_ms"] | (uint32_t)5000;
    g_test_cfg.inter_packet_us = params["inter_packet_us"] | (uint32_t)0;

    g_configured = 1;
    send_ok("configured");
}

static void handle_radio_disable(JsonObject params)
{
    const char *subsystem = "wifi";
    if (params["subsystem"].is<const char *>())
        subsystem = params["subsystem"].as<const char *>();

    if (platform_radio_disable(subsystem) == 0)
        send_ok("radio disabled");
    else
        send_error("failed to disable radio");
}

static void handle_radio_status()
{
    JsonDocument doc;
    doc["status"] = "ok";
    doc["message"] = "radio status";

    JsonObject data = doc["data"].to<JsonObject>();
    data["wifi_active"] = platform_radio_is_active("wifi") > 0;
    data["bluetooth_active"] = platform_radio_is_active("bluetooth") > 0;

    send_json(doc);
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
    send_ok(msg);
}

static void handle_sync(JsonObject params)
{
    if (!g_adapter_ready || !g_active_adapter) {
        send_error("adapter not ready — run WIFI_SETUP/BLE_SETUP first");
        return;
    }

    uint32_t timeout_ms = params["timeout_ms"] | (uint32_t)120000;

    int rc = sync_barrier_wait(g_active_adapter, timeout_ms);
    if (rc != 0) {
        send_error("sync timeout — peer not reachable");
        return;
    }

    send_ok("sync complete");
}

static void handle_start()
{
    if (!g_configured) {
        send_error("not configured");
        return;
    }

    /* Free previous results */
    if (g_results) {
        test_results_free(g_results);
        g_results = nullptr;
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
        send_ok("test complete");
    else
        send_error("test engine failed");
}

static void handle_get_results()
{
    if (!g_results) {
        send_error("no results available");
        return;
    }

    /* Stream results directly via ArduinoJson */
    JsonDocument doc;
    doc["status"] = "ok";
    doc["message"] = "results";

    JsonObject data = doc["data"].to<JsonObject>();

    const char *mode_str = g_test_cfg.mode == TEST_MODE_PING_PONG ? "ping_pong" :
                           g_test_cfg.mode == TEST_MODE_FLOOD     ? "flood"     : "rssi";
    data["mode"] = mode_str;
    data["role"] = g_test_cfg.role == ROLE_SENDER ? "sender" : "receiver";
    data["payload_size"] = g_test_cfg.payload_size;
    data["repetitions"] = g_test_cfg.repetitions;
    data["warmup"] = g_test_cfg.warmup;
    data["packets_sent"] = g_results->packets_sent;
    data["packets_received"] = g_results->packets_received;
    data["packets_lost"] = g_results->packets_lost;
    data["crc_errors"] = g_results->crc_errors;

    JsonArray packets = data["packets"].to<JsonArray>();
    for (uint32_t i = 0; i < g_results->result_count; i++) {
        const packet_result_t *p = &g_results->results[i];
        JsonObject pkt = packets.add<JsonObject>();
        pkt["seq"] = p->seq;
        pkt["tx_us"] = p->tx_us;
        pkt["rx_us"] = p->rx_us;
        pkt["rtt_us"] = p->rtt_us;
        pkt["rssi"] = p->rssi;
        pkt["crc_ok"] = p->crc_ok;
        pkt["lost"] = p->lost;
        pkt["warmup"] = p->is_warmup;
    }

    send_json(doc);
}

static void handle_stop()
{
    if (g_results) {
        test_results_free(g_results);
        g_results = nullptr;
    }
    if (g_adapter_ready && g_active_adapter) {
        g_active_adapter->deinit(g_active_adapter);
        g_adapter_ready = 0;
    }
    g_configured = 0;
    send_ok("stopped");
}

int main()
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

        JsonDocument cmd;
        DeserializationError err = deserializeJson(cmd, line);
        if (err) {
            send_error("invalid JSON");
            continue;
        }

        if (!cmd["command"].is<const char *>()) {
            send_error("missing 'command' field");
            continue;
        }

        const char *command = cmd["command"].as<const char *>();
        JsonObject params = cmd["params"].as<JsonObject>();

        if (strcmp(command, "CONFIGURE") == 0) {
            handle_configure(params);
        } else if (strcmp(command, "RADIO_DISABLE") == 0) {
            handle_radio_disable(params);
        } else if (strcmp(command, "RADIO_STATUS") == 0) {
            handle_radio_status();
        } else if (strcmp(command, "WIFI_SETUP") == 0) {
            handle_link_setup("wifi");
        } else if (strcmp(command, "BLE_SETUP") == 0) {
            handle_link_setup("ble");
        } else if (strcmp(command, "SYNC") == 0) {
            handle_sync(params);
        } else if (strcmp(command, "START") == 0) {
            handle_start();
        } else if (strcmp(command, "GET_RESULTS") == 0) {
            handle_get_results();
        } else if (strcmp(command, "STOP") == 0) {
            handle_stop();
        } else {
            send_error("unknown command");
        }
    }

    /* Cleanup */
    if (g_results)
        test_results_free(g_results);
    if (g_active_adapter)
        g_active_adapter->deinit(g_active_adapter);

    platform_log("agent exiting");
    return 0;
}

/*
 * CPCBF — Arduino MKR WiFi 1010 Agent
 * JSON command-response loop over Serial (USB).
 * Log lines prefixed with '# ' are routed to stderr by the relay.
 * JSON lines (starting with '{') are the command/response channel.
 */
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFiNINA.h>
#include <ArduinoBLE.h>
#include "config.h"

extern "C" {
#include "protocol_adapter.h"
#include "platform_hal.h"
#include "benchmark_packet.h"
#include "test_engine.h"
#include "sync_barrier.h"
}

/* External adapters */
extern "C" protocol_adapter_t wifi_nina_adapter;
extern "C" protocol_adapter_t ble_nina_adapter;

/* Agent state */
static adapter_config_t g_adapter_cfg;
static test_config_t g_test_cfg;
static test_results_t *g_results = nullptr;
static protocol_adapter_t *g_active_adapter = nullptr;
static int g_configured = 0;
static int g_adapter_ready = 0;

/* ---- JSON helpers ---- */

static void send_json(JsonDocument &doc)
{
    serializeJson(doc, Serial);
    Serial.println();
    Serial.flush();
}

static void send_response(const char *status, const char *message)
{
    JsonDocument doc;
    doc["status"] = status;
    if (message) doc["message"] = message;
    send_json(doc);
}

static void send_ok(const char *message)
{
    send_response("ok", message);
}

static void send_error(const char *message)
{
    send_response("error", message);
}

/* ---- Command handlers ---- */

static void handle_configure(JsonObject params)
{
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

    /* Tear down previous adapter if switching protocol */
    if (g_adapter_ready && g_active_adapter) {
        g_active_adapter->deinit(g_active_adapter);
        g_adapter_ready = 0;
    }

    /* Topology selection */
    if (params["topology"].is<const char *>()) {
        const char *topo = params["topology"].as<const char *>();
        if (strcmp(topo, "ble_gatt") == 0)
            g_adapter_cfg.topology = TOPO_BLE_GATT;
        else
            g_adapter_cfg.topology = TOPO_P2P;
    } else {
        g_adapter_cfg.topology = TOPO_P2P;
    }

    /* Protocol/adapter selection */
    if (params["protocol"].is<const char *>() &&
        strcmp(params["protocol"].as<const char *>(), "ble") == 0)
        g_active_adapter = &ble_nina_adapter;
    else
        g_active_adapter = &wifi_nina_adapter;

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

    /* Flood mode: aggregate-only to handle millions of packets without SRAM */
    g_test_cfg.aggregate_only = (g_test_cfg.mode == TEST_MODE_FLOOD) ? 1 : 0;

    g_configured = 1;
    send_ok("configured");
}

static void handle_radio_disable(JsonObject params)
{
    const char *subsystem = "all";
    if (params["subsystem"].is<const char *>())
        subsystem = params["subsystem"].as<const char *>();

    if (strcmp(subsystem, "wifi") == 0 || strcmp(subsystem, "all") == 0)
        WiFi.end();
    if (strcmp(subsystem, "bluetooth") == 0 || strcmp(subsystem, "all") == 0)
        BLE.end();

    send_ok("radio disabled");
}

static void handle_radio_status()
{
    JsonDocument doc;
    doc["status"] = "ok";
    doc["message"] = "radio status";
    JsonObject data = doc["data"].to<JsonObject>();
    /* Report based on which adapter is currently active */
    bool ble_active = (g_active_adapter == &ble_nina_adapter) && g_adapter_ready;
    bool wifi_active = (g_active_adapter == &wifi_nina_adapter) && g_adapter_ready;
    data["wifi_active"] = wifi_active;
    data["bluetooth_active"] = ble_active;
    send_json(doc);
}

static void handle_link_setup()
{
    if (!g_configured || !g_active_adapter) {
        send_error("not configured");
        return;
    }

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
    send_ok("link setup complete");
}

static void handle_sync(JsonObject params)
{
    if (!g_adapter_ready || !g_active_adapter) {
        send_error("adapter not ready — run WIFI_SETUP or BLE_SETUP first");
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

    if (g_results) {
        test_results_free(g_results);
        g_results = nullptr;
    }

    if (!g_adapter_ready) {
        if (!g_active_adapter) g_active_adapter = &wifi_nina_adapter;
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

    /*
     * Stream results field-by-field to avoid building a huge JSON string
     * in RAM. We print the outer envelope and then each packet entry
     * directly to Serial.
     */
    const char *mode_str = g_test_cfg.mode == TEST_MODE_PING_PONG ? "ping_pong" :
                           g_test_cfg.mode == TEST_MODE_FLOOD     ? "flood"     : "rssi";
    const char *role_str = g_test_cfg.role == ROLE_SENDER ? "sender" : "receiver";

    Serial.print("{\"status\":\"ok\",\"message\":\"results\",\"data\":{");
    Serial.print("\"mode\":\""); Serial.print(mode_str); Serial.print("\",");
    Serial.print("\"role\":\""); Serial.print(role_str); Serial.print("\",");
    Serial.print("\"payload_size\":"); Serial.print(g_test_cfg.payload_size); Serial.print(",");
    Serial.print("\"repetitions\":"); Serial.print(g_test_cfg.repetitions); Serial.print(",");
    Serial.print("\"warmup\":"); Serial.print(g_test_cfg.warmup); Serial.print(",");
    Serial.print("\"packets_sent\":"); Serial.print(g_results->packets_sent); Serial.print(",");
    Serial.print("\"packets_received\":"); Serial.print(g_results->packets_received); Serial.print(",");
    Serial.print("\"packets_lost\":"); Serial.print(g_results->packets_lost); Serial.print(",");
    Serial.print("\"crc_errors\":"); Serial.print(g_results->crc_errors); Serial.print(",");
    Serial.print("\"start_us\":"); Serial.print(g_results->start_us); Serial.print(",");
    Serial.print("\"end_us\":"); Serial.print(g_results->end_us); Serial.print(",");
    Serial.print("\"aggregate_only\":"); Serial.print(g_results->aggregate_only); Serial.print(",");
    Serial.print("\"packets\":[");

    for (uint32_t i = 0; i < g_results->result_count; i++) {
        const packet_result_t *p = &g_results->results[i];
        if (i > 0) Serial.print(",");
        Serial.print("{\"seq\":"); Serial.print(p->seq);
        Serial.print(",\"tx_us\":"); Serial.print(p->tx_us);
        Serial.print(",\"rx_us\":"); Serial.print(p->rx_us);
        Serial.print(",\"rtt_us\":"); Serial.print(p->rtt_us);
        Serial.print(",\"rssi\":"); Serial.print(p->rssi);
        Serial.print(",\"crc_ok\":"); Serial.print(p->crc_ok);
        Serial.print(",\"lost\":"); Serial.print(p->lost);
        Serial.print(",\"warmup\":"); Serial.print(p->is_warmup);
        Serial.print("}");
    }

    Serial.println("]}}");
    Serial.flush();
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

/* ---- Arduino entry points ---- */

void setup()
{
    Serial.begin(SERIAL_BAUD);
    while (!Serial) { ; }  /* wait for USB enumeration */

    platform_log("CPCBF agent started on %s", platform_name());
}

void loop()
{
    if (!Serial.available()) return;

    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) return;

    /* Ignore log echo lines */
    if (line.startsWith("#")) return;

    JsonDocument cmd;
    DeserializationError err = deserializeJson(cmd, line);
    if (err) {
        send_error("invalid JSON");
        return;
    }

    if (!cmd["command"].is<const char *>()) {
        send_error("missing 'command' field");
        return;
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
        handle_link_setup();
    } else if (strcmp(command, "BLE_SETUP") == 0) {
        handle_link_setup();
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

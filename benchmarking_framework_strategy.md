# Cross-Platform Communication Benchmarking Framework

## Strategy & Design Document

**Version:** 1.1  
**Date:** 05 March 2026  
**Author:** Tommaso Tragno — FCUL

---

## 1. Executive Summary

This document defines the strategy for building a **Cross-Platform Communication Benchmarking Framework** (CPCBF) — a unified system for measuring and comparing the performance of heterogeneous wireless and wired communication protocols across different hardware platforms. The framework produces fair, reproducible, and statistically rigorous benchmarks of WiFi, LoRa, Bluetooth (BLE), Zigbee, and G.hn, running on Raspberry Pi 4 Model B, Arduino MKR WAN 1300, Arduino MKR WiFi 1010, and the Seeed Wio-WM1110 Dev Kit.

Two core principles guide the design:

1. **Protocol-agnostic test orchestration:** a single test controller defines what to measure and when, while thin, platform-specific adapters handle the how. All raw data flows to a central collector for unified post-processing and visualization.
2. **Homogeneous board pairs:** every test uses two identical boards (e.g., RPi 4 ↔ RPi 4, WM1110 ↔ WM1110). This eliminates hardware asymmetry as a variable, ensuring that measured differences are attributable solely to the protocol and radio under test.

---

## 2. Scope

### 2.1 In Scope

| Dimension | Coverage |
|-----------|----------|
| **Protocols** | WiFi (802.11n/ac), LoRa (SX1262 via WM1110, CMWX1ZZABZ via MKR WAN 1300), Bluetooth Low Energy 5.x, Zigbee 3.0, G.hn (over powerline) |
| **Hardware** | Raspberry Pi 4 Model B (×2), Arduino MKR WAN 1300 (×2), Arduino MKR WiFi 1010 (×2), Seeed Wio-WM1110 Dev Kit (×2), G.hn Powerline adapters (×2) |
| **Metrics** | Latency (RTT), throughput, packet loss ratio, RSSI, jitter |
| **Post-processing** | Statistical analysis, visualization, exportable reports |

### 2.2 Out of Scope

- Protocol-level modifications or custom PHY/MAC implementations.
- Real-time dashboards (post-processing is offline/batch).
- Security benchmarking (encryption overhead is not isolated).
- Cellular protocols (LTE, 5G, NB-IoT).

---

## 3. Test Topology & Constraints

### 3.1 Homogeneous Board Pairs

Every benchmark test uses **two identical boards** — one as sender, one as receiver/echo. The framework never mixes different board types within a single test. This ensures that any performance difference between test runs is attributable to the protocol and radio, not to asymmetric hardware capabilities (different CPUs, different clock speeds, different OS stacks).

Concretely, the required board inventory is:

| Board | Quantity | Integrated Radios Under Test |
|-------|:--------:|------------------------------|
| Raspberry Pi 4 Model B | 2 | WiFi (802.11ac, BCM43455), Bluetooth/BLE (BCM43455) |
| Arduino MKR WAN 1300 | 2 | LoRa (CMWX1ZZABZ-078 + external antenna) |
| Arduino MKR WiFi 1010 | 2 | WiFi (NINA-W102, 802.11b/g/n) |
| Seeed Wio-WM1110 Dev Kit | 2 | LoRa (SX1262), BLE (nRF52840), Zigbee (802.15.4 via nRF52840), WiFi (if supported via external module) |
| G.hn Powerline adapters | 2 | G.hn (tested indirectly — RPi pair communicates over Ethernet through the powerline bridge) |

### 3.2 Protocol–Hardware Mapping

Each board is tested **only on its integrated radio modules** — no external shields or HATs. This keeps results representative of each platform's native capabilities.

| Protocol | RPi 4 Model B | Arduino MKR WAN 1300 | Arduino MKR WiFi 1010 | Seeed WM1110 | Powerline Adapter |
|----------|:-:|:-:|:-:|:-:|:-:|
| **WiFi** | ✅ integrated | ❌ | ✅ integrated | ❌ | ❌ |
| **LoRa** | ❌ | ✅ integrated + ext. antenna | ❌ | ✅ integrated (SX1262) | ❌ |
| **BLE** | ✅ integrated | ❌ | ❌ | ✅ integrated (nRF52840) | ❌ |
| **Zigbee** | ❌ | ❌ | ❌ | ✅ integrated (802.15.4) | ❌ |
| **G.hn** | ✅ via Ethernet bridge | ❌ | ❌ | ❌ | ✅ native |

✅ = tested via integrated module, ❌ = not applicable for this board

This yields the following **concrete test pairings**:

| Test ID | Protocol | Board Pair | Radio Module |
|---------|----------|------------|-------------|
| T1 | WiFi | RPi 4 ↔ RPi 4 | BCM43455 |
| T2 | WiFi | MKR WiFi 1010 ↔ MKR WiFi 1010 | NINA-W102 |
| T3 | BLE | RPi 4 ↔ RPi 4 | BCM43455 |
| T4 | BLE | WM1110 ↔ WM1110 | nRF52840 |
| T5 | LoRa | MKR WAN 1300 ↔ MKR WAN 1300 | CMWX1ZZABZ |
| T6 | LoRa | WM1110 ↔ WM1110 | SX1262 |
| T7 | Zigbee | WM1110 ↔ WM1110 | nRF52840 (802.15.4) |
| T8 | G.hn | RPi 4 ↔ RPi 4 (via PLC adapters) | Powerline bridge |

The analysis pipeline handles this sparse matrix gracefully — not every board tests every protocol, and cross-protocol comparisons are made at the protocol level (aggregating across boards that support that protocol) as well as within the same board (e.g., WM1110 LoRa vs. WM1110 BLE vs. WM1110 Zigbee).

### 3.3 Radio Isolation Requirement

**During any test, the radio module under test must be the only active wireless interface on both boards.** All other radios, network services, and background connections must be disabled to prevent interference, contention for shared RF front-ends, and CPU/bus contention from unrelated traffic.

Specifically, the pre-test setup procedure must:

1. **Disable all unused radios:** on RPi 4, if testing WiFi, disable Bluetooth (`rfkill block bluetooth`) and vice versa. On WM1110, the firmware must initialize only the adapter under test and leave other stacks (BLE SoftDevice, 802.15.4 radio) uninitialized.
2. **Disconnect background services:** on RPi 4, disable `wpa_supplicant` auto-reconnect, `avahi-daemon`, and any service that generates network traffic. The board should not be connected to any access point or paired to any device other than its test partner.
3. **Side-channel exclusion:** the control channel (USB-serial or Ethernet SSH) must use a physically separate interface from the radio under test. For WiFi tests on RPi 4, control goes over USB-serial or Ethernet (not WiFi). For G.hn tests, control goes over WiFi or USB-serial (not the Ethernet/powerline path).
4. **Verification step:** before each test, the controller queries both agents for active interface status and aborts if unexpected connections are detected.

This is enforced both by the agent firmware (which activates only one adapter at a time) and by the controller's pre-flight check.

---

## 4. Requirements

### 4.1 Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| **FR-01** | The system shall execute a configurable test plan specifying protocol, board, payload size, repetition count, and inter-packet interval. | Must |
| **FR-02** | Each test run shall measure round-trip latency (RTT) with microsecond-resolution timestamps where hardware permits. | Must |
| **FR-03** | Each test run shall measure unidirectional throughput (bytes/sec) for configurable payload sizes (32 B – 64 KB). | Must |
| **FR-04** | Each test run shall count transmitted vs. received packets and compute packet loss ratio. | Must |
| **FR-05** | Each test run shall record RSSI (or equivalent link-quality indicator) for every received packet where the radio exposes it. | Must |
| **FR-06** | Each test run shall compute jitter as the standard deviation of inter-arrival times. | Should |
| **FR-07** | A central **Test Controller** shall orchestrate test execution across all connected boards, ensuring synchronized start/stop. | Must |
| **FR-08** | All boards shall report results in a common structured format (JSON lines or CSV). | Must |
| **FR-09** | Raw results shall be collected on a development PC for offline post-processing. | Must |
| **FR-10** | The post-processing pipeline shall produce comparison plots (box plots, CDFs, bar charts) across protocols and boards. | Must |
| **FR-11** | The framework shall support a **ping-pong** test mode (echo server) and a **flood** test mode (one-way stream). | Should |
| **FR-12** | Each test shall log environmental metadata: board ID, firmware version, protocol config, distance, timestamp. | Must |
| **FR-13** | Each test shall use a **homogeneous board pair** — sender and receiver must be the same board model. The test plan validator shall reject configurations pairing different boards. | Must |
| **FR-14** | Before each test, both agents shall **disable all radio interfaces except the one under test** and verify no background network connections exist. The controller shall run a pre-flight check and abort if isolation is not confirmed. | Must |

### 4.2 Non-Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| **NFR-01** | **Fairness:** All protocols shall be tested under identical conditions (same distance, same payload sizes, same number of repetitions) so results are directly comparable. | Must |
| **NFR-02** | **Reproducibility:** Every test run shall be fully described by a declarative configuration file so it can be re-executed identically. | Must |
| **NFR-03** | **Portability:** Board-side test agents shall compile/run on their target without modification beyond a one-time platform config step. | Must |
| **NFR-04** | **Minimal footprint:** Arduino agents shall fit in ≤ 256 KB flash, ≤ 32 KB RAM. RPi and WM1110 agents shall not require desktop-class dependencies. | Must |
| **NFR-05** | **Timing accuracy:** Latency measurements shall have ≤ 1 ms resolution on RPi/WM1110, ≤ 10 µs on Arduino (hardware timer). | Should |
| **NFR-06** | **Extensibility:** Adding a new protocol or board shall require implementing one adapter module without modifying the core test engine. | Should |
| **NFR-07** | **Statistical rigor:** Each test configuration shall run a minimum of 30 repetitions; post-processing shall report mean, median, standard deviation, 95th/99th percentiles, and confidence intervals. | Must |
| **NFR-08** | **Data integrity:** Every transmitted packet shall carry a sequence number and CRC-32 so the receiver can detect loss, duplication, and corruption independently of the transport. | Must |

---

## 5. Technology Stack

### 5.1 Board-Side Agents

| Platform | Language | Build System | Notes |
|----------|----------|-------------|-------|
| **Raspberry Pi 4 Model B** | C (with POSIX sockets) | CMake + cross-compile or native gcc | Integrated WiFi (BCM43455) and BLE (BCM43455); tested on separate runs. BLE via BlueZ/D-Bus. G.hn via Ethernet passthrough to powerline adapter. |
| **Arduino MKR WAN 1300** | C++ (Arduino framework) | PlatformIO | Integrated LoRa (CMWX1ZZABZ-078 + external antenna); library: MKRWAN |
| **Arduino MKR WiFi 1010** | C++ (Arduino framework) | PlatformIO | Integrated WiFi (NINA-W102, u-blox); library: WiFiNINA |
| **Seeed WM1110** | C (nRF5 SDK / Zephyr RTOS) | West (Zephyr build) or nRF Connect SDK | Integrated LoRa (SX1262 via SPI), BLE and Zigbee/802.15.4 (nRF52840 SoftDevice); tested on separate runs per protocol |

### 5.2 Common Test Protocol (Wire Format)

All agents share a **Benchmark Packet** format on the wire:

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|   Magic (0xBE)| Msg Type (1B) |       Sequence Number (2B)    |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                     Timestamp (µs, 4B)                        |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|        Payload Length (2B)    |     Payload (variable)  ...   |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        CRC-32 (4B)                            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

- **Magic byte** `0xBE` identifies benchmark traffic.
- **Msg Type**: `0x01` = PING, `0x02` = PONG (echo), `0x03` = FLOOD data, `0x04` = FLOOD ACK (summary), `0x05` = CTRL (start/stop/config).
- **Timestamp**: sender's local µs counter at transmission; the receiver logs its own reception timestamp independently.
- **CRC-32**: computed over all preceding bytes, providing corruption detection above the link layer.

This fixed header is **13 bytes**, fitting comfortably within the smallest MTUs (LoRa ≈ 222 B, BLE ≈ 244 B, Zigbee ≈ 127 B).

### 5.3 Test Controller

- **Platform:** Development PC (Linux/macOS).
- **Language:** Python 3.11+.
- **Responsibilities:** read test plan YAML, send CTRL commands to agents, collect results, store to disk.
- **Communication with agents:** over a **side-channel** separate from the link under test — typically USB-serial (Arduino, WM1110) or SSH (RPi). This ensures control traffic does not pollute measurements.

### 5.4 Post-Processing & Visualization

| Component | Technology |
|-----------|-----------|
| Data ingestion | Python — pandas |
| Statistical analysis | scipy.stats (confidence intervals, hypothesis tests) |
| Plotting | matplotlib + seaborn |
| Report generation | Jupyter Notebooks or automated PDF via matplotlib `savefig` |
| Data format | SQLite database (one table per test run, plus a metadata table) |

---

## 6. Architecture

### 6.1 High-Level Component Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                     DEVELOPMENT PC                               │
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────────┐   │
│  │ Test         │    │ Data         │    │ Post-Processing   │   │
│  │ Controller   │───▶│ Collector    │───▶│ & Visualization   │   │
│  │ (Python)     │    │ (SQLite)     │    │ (pandas/mpl)      │   │
│  └──────┬───────┘    └──────────────┘    └───────────────────┘   │
│         │  side-channel (USB-serial / SSH)                       │
└─────────┼────────────────────────────────────────────────────────┘
          │
    ┌─────┴─────────────────────────────────────┐
    │              BOARDS UNDER TEST            │
    │                                           │
    │  ┌───────────┐  ┌───────────┐             │
    │  │  Agent A  │  │  Agent B  │             │
    │  │  (sender) │  │ (receiver │             │
    │  │           │  │  / echo)  │             │
    │  └─────┬─────┘  └────┬──────┘             │
    │        │   protocol  │                    │
    │        │◀────link───▶│                    │
    │        │ under test  │                    │
    │                                           │
    └───────────────────────────────────────────┘
```

### 6.2 Component Breakdown

#### 6.2.1 Test Controller (`controller/`)

| Subcomponent | Responsibility |
|---|---|
| `plan_parser.py` | Reads a YAML test plan, validates it against the protocol–hardware matrix, and **rejects any test that pairs different board models**. |
| `agent_manager.py` | Discovers connected agents (USB enumeration + SSH scan), uploads firmware if needed, opens side-channel sessions. |
| `orchestrator.py` | For each test in the plan: configures both agents, **runs radio isolation pre-flight** (disables unused radios, verifies no background connections), issues synchronized START, waits for completion or timeout, issues STOP, collects result files. |
| `radio_isolation.py` | Sends disable/enable commands per radio interface to each agent; queries active interface list; aborts the test if isolation is not confirmed. |
| `clock_sync.py` | Implements a lightweight NTP-like exchange over the side-channel to estimate clock offset between boards (needed for one-way latency). |

#### 6.2.2 Board Agent (`agent/`)

The agent is structured as a layered stack:

```
┌────────────────────────────────┐
│        Test Engine             │  ← protocol-agnostic: ping-pong,
│  (ping-pong, flood, metrics)   │     flood logic, packet counting
├────────────────────────────────┤
│     Protocol Adapter (HAL)     │  ← one per protocol: wifi_adapter,
│  send() / recv() / get_rssi()  │     lora_adapter, ble_adapter, ...
├────────────────────────────────┤
│     Platform Abstraction       │  ← timestamps, serial I/O,
│  timestamp_us(), serial_log()  │     memory allocation
├────────────────────────────────┤
│        Hardware / OS           │
└────────────────────────────────┘
```

**Protocol Adapter interface** (C pseudocode):

```c
typedef struct {
    int  (*init)(const adapter_config_t *cfg);
    int  (*send)(const uint8_t *buf, uint16_t len);
    int  (*recv)(uint8_t *buf, uint16_t max_len, uint32_t timeout_ms);
    int  (*get_rssi)(int8_t *rssi_out);
    void (*deinit)(void);
} protocol_adapter_t;
```

Each protocol (WiFi, LoRa, BLE, Zigbee, G.hn/Ethernet) implements this interface. The test engine calls only these five functions and never touches protocol-specific APIs directly.

**Adapters to implement:**

| Adapter | Platform(s) | Underlying API |
|---------|-------------|----------------|
| `wifi_adapter` | RPi 4, MKR WiFi 1010 | POSIX UDP sockets (RPi), WiFiNINA (MKR WiFi 1010) |
| `lora_adapter` | Arduino MKR WAN 1300, WM1110 | MKRWAN lib (MKR WAN 1300), SX1262 SPI driver (WM1110) |
| `ble_adapter` | RPi 4, WM1110 | BlueZ GATT (RPi), nRF SoftDevice (WM1110) |
| `zigbee_adapter` | WM1110 | IEEE 802.15.4 via nRF52840 radio |
| `ghn_adapter` | RPi 4 (Ethernet side) | POSIX UDP sockets (transparent — G.hn is the medium) |

#### 6.2.3 Data Collector (`collector/`)

Runs on the development PC. Responsibilities:

1. Receives result files (JSON-lines) from each agent via the side-channel after each test completes.
2. Parses, validates (checks sequence numbers, CRC mismatches), and inserts into an SQLite database.
3. Schema:

```sql
CREATE TABLE test_runs (
    run_id       TEXT PRIMARY KEY,
    timestamp    TEXT,
    protocol     TEXT,
    board_sender TEXT,
    board_recv   TEXT,
    payload_size INTEGER,
    repetitions  INTEGER,
    distance_m   REAL,
    config_json  TEXT
);

CREATE TABLE packets (
    run_id       TEXT REFERENCES test_runs(run_id),
    seq          INTEGER,
    tx_us        INTEGER,
    rx_us        INTEGER,
    rtt_us       INTEGER,   -- NULL for flood mode
    rssi         INTEGER,
    crc_ok       BOOLEAN,
    lost         BOOLEAN
);
```

#### 6.2.4 Post-Processing (`analysis/`)

| Module | Output |
|--------|--------|
| `stats.py` | Per-run statistics: mean, median, σ, P95, P99, CI₉₅, packet loss %. |
| `compare.py` | Cross-protocol and cross-board comparison tables. |
| `plots.py` | Box plots (latency by protocol), throughput bar charts, CDF curves, RSSI heatmaps. |
| `report.py` | Generates a summary PDF or Jupyter notebook with all figures and tables. |

---

## 7. Test Methodology

### 7.1 Test Modes

**Ping-Pong (RTT):** Agent A sends a PING; Agent B immediately echoes it as a PONG. Agent A records the round-trip time. This measures latency without needing clock synchronization.

**Flood (Throughput + Loss):** Agent A sends N packets as fast as the protocol allows. Agent B counts received packets, logs arrival timestamps, and sends a single FLOOD ACK summary at the end. This measures throughput and packet loss.

### 7.2 Test Plan Structure (YAML)

```yaml
# test_plan.yaml
global:
  repetitions: 100        # packets per test
  warmup: 10              # discard first N packets
  cooldown_sec: 5         # pause between tests
  distance_m: 1.0         # physical separation
  radio_isolation: true   # enforce: disable all radios except the one under test

tests:
  # --- WiFi Tests (homogeneous pairs) ---
  - name: "wifi_rtt_rpi4"
    protocol: wifi
    mode: ping_pong
    sender: rpi4_a
    receiver: rpi4_b                   # same board model
    payload_sizes: [32, 128, 512, 1024, 4096]
    pre_test_disable: [bluetooth]      # rfkill block bluetooth on both boards

  - name: "wifi_rtt_mkr1010"
    protocol: wifi
    mode: ping_pong
    sender: mkr_wifi_1010_a
    receiver: mkr_wifi_1010_b          # same board model
    payload_sizes: [32, 128, 512, 1024]

  # --- LoRa Tests (homogeneous pairs) ---
  - name: "lora_throughput_mkr1300"
    protocol: lora
    mode: flood
    sender: mkr_wan_1300_a
    receiver: mkr_wan_1300_b           # same board model
    payload_sizes: [32, 64, 128]
    repetitions: 500                   # override: LoRa is slow

  - name: "lora_rtt_wm1110"
    protocol: lora
    mode: ping_pong
    sender: wm1110_a
    receiver: wm1110_b                 # same board model
    payload_sizes: [32, 64, 128]
    pre_test_disable: [ble, zigbee]    # only LoRa radio active

  # --- BLE Tests (homogeneous pairs) ---
  - name: "ble_rtt_rpi4"
    protocol: ble
    mode: ping_pong
    sender: rpi4_a
    receiver: rpi4_b                   # same board model
    payload_sizes: [20, 100, 244]
    pre_test_disable: [wifi]           # rfkill block wifi on both boards

  - name: "ble_rtt_wm1110"
    protocol: ble
    mode: ping_pong
    sender: wm1110_a
    receiver: wm1110_b                 # same board model
    payload_sizes: [20, 100, 244]
    pre_test_disable: [lora, zigbee]   # only BLE radio active

  # --- Zigbee Tests (homogeneous pair) ---
  - name: "zigbee_rtt_wm1110"
    protocol: zigbee
    mode: ping_pong
    sender: wm1110_a
    receiver: wm1110_b                 # same board model
    payload_sizes: [20, 64, 100]
    pre_test_disable: [lora, ble]      # only 802.15.4 radio active

  # --- G.hn Tests (homogeneous pair, indirect) ---
  - name: "ghn_throughput"
    protocol: ghn
    mode: flood
    sender: rpi4_a                     # connected via powerline adapter A
    receiver: rpi4_b                   # connected via powerline adapter B
    payload_sizes: [128, 512, 1024, 4096, 65536]
    pre_test_disable: [wifi, bluetooth] # only Ethernet active on both RPis
```

### 7.3 Execution Procedure

1. **Environment setup:** Place the two identical boards at a fixed, measured distance. Power cycle all radios. Record ambient conditions (optional: WiFi channel survey for interference baseline).
2. **Radio isolation:** The controller sends a CTRL command to both agents instructing them to disable all radios except the one under test. Each agent confirms isolation status (list of active/inactive interfaces). The controller aborts if any unexpected interface is active or if background connections are detected.
3. **Clock calibration:** The controller runs `clock_sync` over the side-channel to estimate clock offset between the two boards (only needed for one-way metrics in flood mode).
4. **Warmup:** Each test begins with `warmup` packets that are transmitted but excluded from analysis, allowing protocol-level link establishment and buffering to stabilize.
5. **Test execution:** The controller sends CTRL-START to both agents simultaneously (within side-channel jitter). Agents execute the specified mode. On completion (all packets sent + timeout for last reply), agents report results over the side-channel.
6. **Cooldown:** A pause between consecutive tests prevents thermal or buffer-related carryover effects.
7. **Multi-distance runs (optional):** Repeat the entire plan at 1 m, 5 m, 10 m, 20 m to capture range-dependent behavior.

### 7.4 Fairness Controls

- **Homogeneous board pairs:** sender and receiver are always the same board model. The plan validator rejects mixed-board configurations.
- **Radio isolation:** only the radio under test is active; all other wireless interfaces are disabled and verified before each run (see Section 3.3).
- **No background connections:** boards are not connected to any access point, cloud service, or paired device other than their test partner during execution.
- **Same payload sizes** across protocols (capped at each protocol's MTU — if a payload exceeds MTU, the framework skips that data point and logs a warning).
- **Same repetition count** (or a justified minimum per protocol — e.g., LoRa's duty-cycle limits may cap practical repetitions).
- **Same physical environment** for all tests in a plan execution (same room, same time window).
- **Sequential, not concurrent** execution: only one protocol is active at a time to prevent cross-interference.
- **Identical measurement code path:** the test engine's packet-creation, timestamping, and logging code is shared across all adapters.

---

## 8. Development Methodology

### 8.1 Phased Approach

| Phase | Duration | Deliverables |
|-------|----------|-------------|
| **Phase 1 — Core Framework** | 2 weeks | Benchmark packet codec (C), test engine (ping-pong + flood), side-channel serial protocol, basic controller. |
| **Phase 2 — First Protocol (WiFi)** | 1 week | WiFi adapter for RPi 4, end-to-end ping-pong test, data collection into SQLite. |
| **Phase 3 — Additional Protocols** | 3 weeks | LoRa adapter (MKR WAN 1300 + WM1110), BLE adapter (RPi 4 + WM1110), Zigbee adapter (WM1110), WiFi adapter (MKR WiFi 1010), G.hn adapter (RPi-to-RPi via powerline). |
| **Phase 4 — Post-Processing** | 1 week | pandas ingestion, statistical analysis, matplotlib plots, comparison tables. |
| **Phase 5 — Validation & Reporting** | 1 week | Cross-check results against known baselines (e.g., WiFi throughput should match iperf), finalize report template. |

### 8.2 Version Control & CI

- **Repository:** Git (monorepo), with `agent/`, `controller/`, `analysis/`, `docs/` top-level directories.
- **Branching:** trunk-based development; short-lived feature branches per adapter.
- **CI (optional):** GitHub Actions running unit tests for the packet codec (host-compiled with gcc) and the Python controller/analysis modules.

### 8.3 Testing Strategy

| Level | What | How |
|-------|------|-----|
| **Unit** | Packet encode/decode, CRC-32, stats calculations | Host-compiled C tests (Unity framework or plain asserts), pytest for Python |
| **Integration** | Agent ↔ Controller side-channel communication | Loopback test: USB-serial with a single board acting as both sender and receiver |
| **System** | Full end-to-end benchmark run | Execute a minimal test plan (WiFi ping-pong, 10 reps) and verify SQLite output matches expected structure |
| **Validation** | Result plausibility | Compare WiFi RTT against `ping`, throughput against `iperf3`; LoRa throughput against datasheet maximums |

---

## 9. Codebase Layout

```
cpcbf/
├── agent/
│   ├── common/
│   │   ├── benchmark_packet.h / .c      # encode/decode/CRC
│   │   ├── test_engine.h / .c           # ping-pong, flood logic
│   │   ├── platform_hal.h               # timestamp, serial, malloc
│   │   └── protocol_adapter.h           # adapter interface
│   ├── adapters/
│   │   ├── wifi_adapter.c
│   │   ├── lora_adapter.c
│   │   ├── ble_adapter.c
│   │   ├── zigbee_adapter.c
│   │   └── ghn_adapter.c               # (Ethernet passthrough)
│   ├── platforms/
│   │   ├── rpi4/                        # platform_hal impl + CMakeLists
│   │   ├── arduino_mkr_wan1300/         # PlatformIO project (LoRa)
│   │   ├── arduino_mkr_wifi1010/        # PlatformIO project (WiFi)
│   │   └── wm1110/                      # Zephyr / nRF Connect project
│   └── tests/
│       └── test_packet_codec.c
├── controller/
│   ├── plan_parser.py
│   ├── agent_manager.py
│   ├── orchestrator.py
│   ├── clock_sync.py
│   ├── serial_transport.py
│   └── ssh_transport.py
├── analysis/
│   ├── ingest.py                        # JSON-lines → SQLite
│   ├── stats.py
│   ├── compare.py
│   ├── plots.py
│   └── report.py
├── plans/
│   ├── full_sweep.yaml
│   └── quick_smoke.yaml
├── docs/
│   └── strategy.md                      # this document
├── pyproject.toml
└── README.md
```

---

## 10. Metrics Definition

| Metric | Definition | Unit | Computation |
|--------|-----------|------|-------------|
| **RTT Latency** | Time from PING transmission to PONG reception at the sender. | µs | `rx_timestamp_sender − tx_timestamp_sender` |
| **One-way Latency** | Time from sender TX to receiver RX (requires clock sync). | µs | `rx_timestamp_receiver − tx_timestamp_sender − clock_offset` |
| **Throughput** | Useful payload bytes received per unit time during flood mode. | bytes/s | `Σ payload_bytes_received / (last_rx_time − first_rx_time)` |
| **Packet Loss** | Fraction of sent packets not received. | % | `(packets_sent − packets_received) / packets_sent × 100` |
| **RSSI** | Received Signal Strength Indicator, as reported by the radio. | dBm | Read from adapter `get_rssi()` per packet |
| **Jitter** | Variability of inter-arrival times. | µs | `stddev(rx_time[i+1] − rx_time[i])` for consecutive received packets |

---

## 11. Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| LoRa duty-cycle regulations limit test repetitions | Fewer data points, wider confidence intervals | High | Use unlicensed test modes where legal; increase per-packet payload to extract more data per transmission; document regulatory constraints |
| BLE connection interval variability adds latency noise | Noisy RTT measurements | Medium | Request minimum connection interval (7.5 ms) on both ends; report connection parameters in metadata |
| Clock drift between boards corrupts one-way latency | Inaccurate one-way metrics | Medium | Rely primarily on RTT (no sync needed); use clock_sync only as a supplement; re-sync before each test run |
| Arduino memory limits prevent large payloads | Cannot test large payload sizes on constrained boards | Low | Cap payload at platform MTU; document the limitation; compare only at overlapping sizes |
| G.hn adapter variability (vendor-specific chipsets) | Non-reproducible results across different adapter models | Medium | Standardize on a single adapter model; record adapter firmware version in metadata |

---

## 12. Deliverables Checklist

- [ ] Benchmark packet codec (C library, host-testable)
- [ ] Test engine (C library, ping-pong + flood)
- [ ] Protocol adapters: WiFi, LoRa, BLE, Zigbee, G.hn
- [ ] Platform builds: RPi 4, Arduino MKR WAN 1300, Arduino MKR WiFi 1010, WM1110
- [ ] Python test controller with YAML plan parser
- [ ] Data collector and SQLite schema
- [ ] Post-processing pipeline (stats, plots, comparison)
- [ ] Validation against reference tools (ping, iperf3)
- [ ] Documentation: this strategy doc, README, per-platform setup guides

---

## 13. References

- IEEE 802.11 (WiFi), IEEE 802.15.4 (Zigbee), Bluetooth Core Specification 5.x
- ITU-T G.9960 (G.hn)
- Semtech SX1262 datasheet (LoRa transceiver on WM1110)
- Nordic nRF52840 Product Specification (WM1110 MCU)
- PlatformIO documentation (Arduino builds)
- Zephyr RTOS documentation (WM1110 builds)
- RFC 2544 — Benchmarking Methodology for Network Interconnect Devices (conceptual reference for test methodology)

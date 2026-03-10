# CPCBF Architecture Diagrams

## 1. System Architecture

```mermaid
graph TB
    subgraph "Controller Workstation"
        CLI["cpcbf CLI"]
        ORCH["Orchestrator"]
        PLAN["Plan Parser<br/><i>YAML plans</i>"]
        SSH["SSH Transport"]
        CLOCK["Clock Sync"]
        RADIO["Radio Isolation"]
        CLI --> ORCH
        ORCH --> PLAN
        ORCH --> SSH
        ORCH --> CLOCK
        ORCH --> RADIO
    end

    subgraph "RPi4-A (Sender / GO)"
        AGENT_A["Agent Binary<br/><i>C, JSON stdin/stdout</i>"]
        WIFI_A["WiFi Adapter<br/><i>UDP sockets</i>"]
        SETUP_A["WiFi Setup<br/><i>P2P / ad-hoc</i>"]
        HAL_A["Platform HAL<br/><i>timestamps, logging</i>"]
        ENGINE_A["Test Engine<br/><i>ping-pong / flood</i>"]
        AGENT_A --> ENGINE_A
        ENGINE_A --> WIFI_A
        WIFI_A --> SETUP_A
        AGENT_A --> HAL_A
    end

    subgraph "RPi4-B (Receiver / Client)"
        AGENT_B["Agent Binary"]
        WIFI_B["WiFi Adapter"]
        ENGINE_B["Test Engine"]
        AGENT_B --> ENGINE_B
        ENGINE_B --> WIFI_B
    end

    SSH -- "JSON over SSH<br/>(Ethernet)" --> AGENT_A
    SSH -- "JSON over SSH<br/>(Ethernet)" --> AGENT_B
    WIFI_A <-- "UDP packets<br/>(Wi-Fi Direct)" --> WIFI_B

    subgraph "Analysis Pipeline"
        INGEST["Ingest<br/><i>JSONL → SQLite</i>"]
        STATS["Stats<br/><i>RTT, throughput, CI</i>"]
        PLOTS["Plots<br/><i>boxplot, CDF, bar</i>"]
        COMPARE["Compare<br/><i>cross-run table</i>"]
        INGEST --> STATS --> PLOTS
        STATS --> COMPARE
    end

    ORCH -- "results.jsonl" --> INGEST
```

## 2. Agent Internal Architecture

```mermaid
graph LR
    STDIN["stdin<br/><i>JSON commands</i>"] --> MAIN["main.c<br/><i>command loop</i>"]
    MAIN --> |CONFIGURE| CFG["adapter_config_t<br/>test_config_t"]
    MAIN --> |WIFI_SETUP| ADAPTER["protocol_adapter_t"]
    MAIN --> |START| ENGINE["test_engine_run()"]
    MAIN --> |GET_RESULTS| SERIAL["test_results_to_json()"]
    MAIN --> |STOP| DEINIT["adapter.deinit()"]
    MAIN --> STDOUT["stdout<br/><i>JSON responses</i>"]

    ENGINE --> ADAPTER
    ADAPTER --> INIT["wifi_init()<br/><i>UDP socket + bind</i>"]
    ADAPTER --> SEND["wifi_send()<br/><i>sendto()</i>"]
    ADAPTER --> RECV["wifi_recv()<br/><i>recvfrom()</i>"]
    ADAPTER --> RSSI["wifi_get_rssi()<br/><i>wpa_cli signal_poll</i>"]

    subgraph "WiFi Setup"
        SETUP_FN["wifi_setup()"]
        CLEANUP["full_cleanup()<br/><i>stop NetworkManager<br/>kill wpa_supplicant</i>"]
        P2P_GO["P2P GO<br/><i>p2p_group_add</i>"]
        P2P_CLI["P2P Client<br/><i>p2p_find + p2p_connect</i>"]
        ADHOC["Ad-hoc<br/><i>iw ibss join</i>"]
        SETUP_FN --> CLEANUP
        SETUP_FN --> P2P_GO
        SETUP_FN --> P2P_CLI
        SETUP_FN --> ADHOC
    end

    INIT --> SETUP_FN
```

## 3. Packet Wire Format

```mermaid
graph LR
    subgraph Header ["Header (10 bytes)"]
        A["Magic<br/>0xBE<br/><i>1B</i>"]
        B["MsgType<br/><i>1B</i>"]
        C["SeqNum<br/><i>2B</i>"]
        D["Timestamp<br/><i>4B (us)</i>"]
        E["PayloadLen<br/><i>2B</i>"]
        A --- B --- C --- D --- E
    end
    subgraph Payload [" "]
        F["Payload<br/><i>variable</i>"]
    end
    subgraph Trailer ["Trailer (4 bytes)"]
        G["CRC-32<br/><i>4B</i>"]
    end
    E --- F --- G

    style A fill:#4a9,color:#fff
    style G fill:#e74,color:#fff
```

## 4. Test Modes

```mermaid
sequenceDiagram
    participant S as Sender (GO)
    participant R as Receiver (Client)

    Note over S,R: Ping-Pong Mode (RTT)
    loop repetitions + warmup
        S->>R: UDP packet (seq N)
        R->>S: Echo packet (seq N)
        Note right of S: RTT = rx_us - tx_us
    end

    Note over S,R: Flood Mode (Throughput)
    S->>R: UDP packet (seq 0)
    S->>R: UDP packet (seq 1)
    S->>R: UDP packet (seq 2)
    S->>R: ...
    S->>R: UDP packet (seq N)
    Note right of R: Throughput = bits / duration
    Note right of R: Loss = sent - received
```

## 5. Lab Mode vs Field Mode

```mermaid
graph TB
    subgraph LAB ["Lab Mode -- Ethernet available"]
        WS["Workstation"] -- "SSH over Ethernet" --> RPI_A1["RPi4-A"]
        WS -- "SSH over Ethernet" --> RPI_B1["RPi4-B"]
        WS -- "runs" --> CPCBF_CLI["cpcbf CLI + orchestrator"]
        CPCBF_CLI -- "writes" --> JSONL1["results/results.jsonl"]
        JSONL1 --> ANALYSIS1["run_analysis.py"]
    end

    subgraph FIELD ["Field Mode -- no Ethernet"]
        RPI_A2["RPi4-A<br/>systemd + auto_bench.py"] -- "boot" --> AGENT_A2["agent - sender"]
        RPI_B2["RPi4-B<br/>systemd + auto_bench.py"] -- "boot" --> AGENT_B2["agent - receiver"]
        AGENT_A2 -- "Wi-Fi Direct P2P" --> AGENT_B2
        AGENT_B2 -- "Wi-Fi Direct P2P" --> AGENT_A2
        AGENT_A2 -- "writes" --> JSONL_A["field/results/*_sender.jsonl"]
        AGENT_B2 -- "writes" --> JSONL_B["field/results/*_receiver.jsonl"]
        JSONL_A -- "scp" --> MERGE["merge_results.py"]
        JSONL_B -- "scp" --> MERGE
        MERGE -- "results.jsonl" --> ANALYSIS2["run_analysis.py"]
    end
```

## 6. Data Pipeline

```mermaid
graph LR
    YAML["YAML Plan<br/><i>modes, payloads,<br/>repetitions</i>"]
    YAML --> EXEC["Test Execution<br/><i>agent on each RPi</i>"]
    EXEC --> JSONL["JSONL<br/><i>per-packet data:<br/>seq, rtt, rssi, lost</i>"]
    JSONL --> INGEST["ingest.py<br/><i>JSONL → SQLite</i>"]
    INGEST --> DB[("benchmark.db<br/><i>test_runs<br/>packets</i>")]
    DB --> STATS["stats.py<br/><i>mean, median, P95,<br/>P99, CI, jitter</i>"]
    DB --> PLOTS["plots.py"]
    STATS --> CSV["stats.csv<br/>comparison.csv"]
    PLOTS --> IMG["rtt_boxplot.png<br/>rtt_cdf.png<br/>throughput.png<br/>loss.png<br/>rssi.png"]
```

## 7. Project File Structure

```mermaid
graph TD
    ROOT["cpcbf/"]
    ROOT --> AGENT["agent/<br/><i>C agent binary</i>"]
    ROOT --> CTRL["controller/<br/><i>Python SSH orchestrator</i>"]
    ROOT --> ANAL["analysis/<br/><i>ingest, stats, plots</i>"]
    ROOT --> FIELD["field/<br/><i>autonomous mode</i>"]
    ROOT --> PLANS["plans/<br/><i>YAML test configs</i>"]
    ROOT --> RUN["run_analysis.py"]

    AGENT --> COMMON["common/<br/><i>benchmark_packet</i><br/><i>test_engine</i><br/><i>protocol_adapter</i><br/><i>platform_hal</i><br/><i>cjson/</i>"]
    AGENT --> PLAT["platforms/rpi4/<br/><i>main.c</i><br/><i>platform_rpi4.c</i><br/><i>wifi_setup.c</i>"]
    AGENT --> ADAPT["adapters/<br/><i>wifi_adapter.c</i>"]
    AGENT --> TESTS["tests/<br/><i>test_packet_codec.c</i>"]

    CTRL --> CLI_PY["cli.py"]
    CTRL --> ORCH_PY["orchestrator.py"]
    CTRL --> SSH_PY["ssh_transport.py"]

    FIELD --> AUTO["auto_bench.py"]
    FIELD --> MERGE["merge_results.py"]
    FIELD --> DEPLOY["deploy.sh"]
    FIELD --> SERVICE["cpcbf-auto.service"]
    FIELD --> ROLES["role_sender/receiver.json"]
```

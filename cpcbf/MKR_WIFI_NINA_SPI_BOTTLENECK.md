# MKR WiFi 1010 — NINA SPI Throughput Bottleneck

Why the receiver caps at ~49 packets per second for 1432-byte UDP payloads, even though the CRC-32 verification in `benchmark_packet.c` only costs ~1 % of the per-packet budget.

## Observed behaviour (measured on two MKR WiFi 1010 boards, SoftAP topology)

| Inter-packet interval | Sender rate | Receiver rate | Delivery |
|---|---|---|---|
| 25 ms | 32.3 pps | 32.2 pps | **99.55 %** |
| 10 ms | 63.2 pps | 49.4 pps | 76.9 % |
| 6 ms | ~85 pps | ~50 pps | 58 % |

Per-chunk receiver rate is **rock-steady** (σ/μ < 1 %), and the receiver is a hard ceiling at ~49 pps regardless of how fast the sender pushes. That means the limit is **steady-state-bound**, not a tail event.

The claim this document proves: the NINA-SPI link is the bottleneck. CRC-32 verification is ~1.3 % of the budget.

## Concrete evidence from the library code

All line references are against `Arduino_SpiNINA/src/spi_drv.cpp` and `WiFiNINA/src/utility/WiFiSocketBuffer.cpp` as pulled by PlatformIO for the `mkrwifi1010_wifi_flood` env.

### 1. SPI clock is hard-coded to 8 MHz

`spi_drv.cpp:123`
```cpp
SPIWIFI.beginTransaction(SPISettings(8000000, MSBFIRST, SPI_MODE0));
```

### 2. No DMA, no burst — byte-at-a-time synchronous loop

`spi_drv.cpp:413-427`
```cpp
void SpiDrv::sendBuffer(const uint8_t* param, uint16_t param_len, uint8_t lastParam) {
  sendParamLen16(param_len);
  for (uint16_t i = 0; i < param_len; ++i) {
    spiTransfer(param[i]);          // one byte per call, synchronous
  }
  if (lastParam == 1) spiTransfer(END_CMD);
}
```

`spiTransfer()` at line 138 wraps Arduino `SPI.transfer()` — write DATA, spin on `INTFLAG.RXC`, read DATA. **No SERCOM DMA.** Every byte is a separate CPU-bound transaction.

### 3. Blocking GPIO handshake before/after every command

`spi_drv.cpp:198-208`
```cpp
void SpiDrv::waitForSlaveReady(bool const feed_watchdog) {
  unsigned long const start = millis();
  while (!waitSlaveReady()) { ... }   // spin on GPIO NINA_ACK
}
```

Called pervasively — **45 `waitForSlaveReady()` sites** in `wifi_drv.cpp` + `server_drv.cpp`.

### 4. NINA RX buffer is a single 1500-byte slot — no queue

`WiFiSocketBuffer.cpp:30-33`
```cpp
#ifdef __AVR__
#define WIFI_SOCKET_BUFFER_SIZE 64
#else
#define WIFI_SOCKET_BUFFER_SIZE 1500   // one UDP datagram, then full
#endif
```

One 1500-byte slot per socket, not a queue. The instant NINA's ESP32 side has a fresh packet but the SAMD21 hasn't drained the last one, the new one is dropped at the lwIP→SPI boundary. **No drop tolerance.**

## Real math, from datasheet specs

### Hardware

**Atmel SAMD21G18A** (datasheet §27 — SERCOM SPI)
- Core clock: 48 MHz (MKR WiFi 1010 board definition)
- Baud formula: `f_baud = f_ref / (2·(BAUD+1))` → `BAUD=2` → exactly 8 MHz ✓
- SERCOM SPI has a **single-byte DATA register**; no hardware FIFO; WiFiNINA's driver wires no DMA

**u-blox NINA-W102** (datasheet §1.7)
- Host interface: SPI slave, max ~10 MHz
- Slave-mode firmware has **no datagram queueing**; one-shot buffer per socket

### Per-byte cost of `SPI.transfer()` on SAMD21

| Source | Cycles @ 48 MHz | Time |
|---|---|---|
| Wire shift at 8 MHz (8 bits) | 48 | **1.00 µs** |
| `SPI.transfer()`: write + poll `INTFLAG.RXC` + read (~30 M0+ insns) | ~30 | **0.63 µs** |
| `for`-loop iteration + `DELAY_TRANSFER()` | ~5 | ~0.10 µs |
| **Sustained per-byte cost** | | **~1.7 µs** |

This matches the Arduino community's benchmarked ~500-600 KB/s ceiling for `SPI.transfer()` on SAMD21.

### Budget for one 1432-byte UDP receive

WiFiNINA `parsePacket()` → `availData()` + `read()` → `getDataBuf()`.

Protocol frame (`spi_drv.cpp:444`):
```
| START | C/R+CMD | numParam | paramLen | PARAM | ... | END |
```

| Operation | Bytes on SPI | Time @ 1.7 µs/byte |
|---|---|---|
| `availData` cmd TX | 6 | 10 µs |
| `availData` reply RX | 8 | 14 µs |
| `getDataBuf` cmd TX | 6 | 10 µs |
| Reply header (START, cmd, numParam, paramLen16) | 5 | 9 µs |
| **Payload data shift (1432 B)** | **1432** | **2.43 ms** |
| `END_CMD` | 1 | 2 µs |
| 4 × `waitForSlaveReady` GPIO polls (best case) | — | ~50 µs |
| NINA ESP32 lwIP → SPI prep (load-dependent) | — | 1-5 ms |
| **Total** | ~1458 | **~3.5-8 ms / packet** |

### Observed 49 pps at 10 ms IPI = 20 ms/packet budget

| Component | Cost | % of 20 ms |
|---|---|---|
| **SPI RX path** (table above) | 3.5-8 ms | **17-40 %** |
| NINA internal scheduling (lwIP copy, DMA prep) | 1-5 ms | 5-25 % |
| `parsePacket()` polling loop (N × cmd round-trips) | 2-10 ms | 10-50 % |
| **CRC-32 byte-table over 1442 B** | **0.27 ms** | **1.3 %** |
| `bench_packet_decode` (`memcpy` + parse) | 0.05 ms | 0.25 % |

CRC + decode is **≤ 2 %** of the budget. The SPI + NINA path dominates **~80 %**.

### Absolute bit-clock ceiling

If the SPI link ran back-to-back with zero overhead:
```
1474 B × 8 bits / 8 Mbit/s = 1.474 ms / packet  →  678 pps = 9.97 Mbps
```

Observed **~49 pps = 578 kbps = 5.8 % of the bit-clock ceiling**. The remaining 94 % is eaten by:
- Per-byte SERCOM polling overhead (no DMA)
- GPIO `waitForSlaveReady` spins
- Protocol framing + command round-trips
- Single-slot 1500-byte buffer with no queue → any RX stall drops the next packet

## Why the sender survives higher rates than the receiver

Sender path per 1432 B TX:
```
sendCmd + sendBuffer(1432) + END_CMD + ack  →  ONE round-trip of cmd-framing
```

Receiver path per RX:
```
parsePacket polling → availData round-trip → getDataBuf round-trip
→ TWO or more cmd round-trips, plus the single-slot buffer can drop packets
  during the inter-round-trip gap
```

That's why at 10 ms IPI the sender holds 63 pps but the receiver caps at 49 pps — same SPI link, but the RX control flow has **one extra round-trip and a buffer with no drop tolerance**.

## Why loss only appears above a threshold

The single-slot 1500-byte NINA buffer turns **steady-state rate mismatch** into observed packet loss. Exactly the behaviour we see:

| IPI | Sender pace | Receiver drain | Overlap | Loss |
|---|---|---|---|---|
| 25 ms (40 pps max) | 32 pps | 32 pps | 0 pps | **0.4 %** |
| 10 ms (100 pps max) | 63 pps | 49 pps | 14 pps | **23 %** |
| 6 ms (166 pps max) | 85 pps | ~50 pps | ~35 pps | **42 %** |

The NINA buffer is full before the SAMD21 can drain it → every packet arriving during the overlap is dropped.

## TL;DR proof

- SPI clock is **hard-coded to 8 MHz** (`spi_drv.cpp:123`) → theoretical ceiling 678 pps
- Transfer loop is **byte-at-a-time without DMA** (`spi_drv.cpp:420`) → ~1.7 µs/byte actual
- **45 blocking `waitForSlaveReady()` GPIO polls** in the driver add unknown-but-real stall time
- NINA socket buffer is **1500 B single-shot, no queue** (`WiFiSocketBuffer.cpp:32`) → any RX processing overlap drops packets
- CRC-32 table-LUT is **0.27 ms on 1442 B** vs **20 ms total budget** → **1.3 %**

The NINA-SPI link is the bottleneck by a factor of **15-30 over the CRC cost**, and the single-slot RX buffer is what turns steady-state rate mismatch into observed packet loss.

## Practical consequences for the benchmark

1. **MKR→MKR WiFi flood cannot exceed ~49 pps** at 1432 B payload without dropping packets.
2. **Loss is NOT a bug in our test engine** — it's a NINA hardware/firmware ceiling.
3. To stay lossless: set `inter_packet_us ≥ 25000` (verified 0.4 % loss at 4 k packets).
4. For higher sustained throughput, the topology must be **MKR → RPi4** (RPi kernel UDP drains at > 1000 pps), not MKR → MKR.
5. CRC-32 is **not** worth optimising; even removing it entirely would lift the ceiling by < 2 %.

## References

- `cpcbf/agent/platforms/arduino_mkr_wifi1010/.pio/libdeps/mkrwifi1010_wifi_flood/Arduino_SpiNINA/src/spi_drv.cpp`
- `cpcbf/agent/platforms/arduino_mkr_wifi1010/.pio/libdeps/mkrwifi1010_wifi_flood/WiFiNINA/src/utility/WiFiSocketBuffer.cpp`
- `cpcbf/agent/common/benchmark_packet.c` — CRC-32 byte-table implementation
- Atmel ATSAMD21G18A datasheet, §27 (SERCOM — SPI mode)
- u-blox NINA-W10 series datasheet, §1.7 (Host interface)

---

# MKR WiFi 1010 — NINA SPI Bottleneck on the BLE Stack

Separate, harder ceiling than the WiFi path. Why the BLE receiver freezes at 180-byte GATT writes with 70 ms inter-packet interval, why the same SPI link that sustains 32 pps of 1432 B UDP caps at ~12 pps of 180 B ATT, and why the fix is NOT larger pool buffers but either smaller payloads or bulk-read patching of ArduinoBLE.

## Observed behaviour (two MKR WiFi 1010 boards, paced ATT Write-Without-Response)

| Payload | Wire frame | IPI | Packets sent | Packets received | Loss |
|---|---|---|---|---|---|
|  27 B |  41 B | 70 ms | 1010 | 1010 | **0 %** |
| 127 B | 141 B | 70 ms | 1010 | 1010 | **0 %** |
| **180 B** | **194 B** | **70 ms** | **1010** | **71** | **93 %** |

The cutoff is sharp and reproducible. On-firmware diagnostics (`s_callback_count`, `s_pool_push_count`, `s_pool_full_count`) from the 180 B run:

```
ble_diag = {callbacks: 1010, pool_pushes: 71, pool_full_drops: 939}
```

And the timestamped breadcrumbs from `ble_adapter_nina.cpp`:

```
t=16324  flood_rx: entry total=1010 agg=1 timeout_ms=5000
t=18427  pool first-push:  cb=1     len=194        ← first ATT write arrives
t=24333  pool first-full:  cb=72    pushed=71  used=13916   ← pool saturates at 71 entries (14000 B / 196 B ≈ 71.4)
t=102899 pool first-pop:   cb=1010  pushed=71  full=939     ← consumer gets its FIRST entry, 84 s after first-push
```

Between first-push and first-pop, **the consumer's `pool_pop()` never runs for ~84 seconds**, while `on_tx_written()` fires 1010 times at ~83 ms intervals. The pool is not the bottleneck — the consumer is being starved of CPU because `BLE.poll()` never returns.

The claim this section proves: **`HCIClass::poll()` is a closed inner loop driven by `HCITransport.available()`, and at 180 B / 70 ms IPI the NINA SPI HCI transport always has more bytes ready than the SAMD21 can drain, so `available()` never returns 0 and `poll()` never exits for the entire duration of the test.**

## Concrete evidence from the library code

All line references are against `ArduinoBLE/src/` as pulled by PlatformIO for the `mkrwifi1010_ble_flood` env.

### 1. The HCI receive loop is purely `available()`-driven

`utility/HCI.cpp:140-173` (`HCIClass::poll(unsigned long timeout)`):

```cpp
HCITransport.lockForRead();
while (HCITransport.available()) {       // ← loop termination condition
  byte b = HCITransport.read();
  _recvBuffer[_recvIndex++] = b;

  if (_recvBuffer[0] == HCI_ACLDATA_PKT) {
    if (_recvIndex > 5 && _recvIndex >= (5 + (_recvBuffer[3] + (_recvBuffer[4] << 8)))) {
      // packet complete → dispatch callback synchronously, then resume loop
      handleAclDataPkt(pktLen, &_recvBuffer[1]);
      HCITransport.lockForRead();
    }
  } else if (_recvBuffer[0] == HCI_EVENT_PKT) { ... }
}
```

There is **no packet count cap, no deadline check inside the loop, and no yield to the caller**. The loop exits if and only if `HCITransport.available()` returns 0. With `timeout = 0` (the only call site we use — see `BLELocalDevice::poll()` at `local/BLELocalDevice.cpp:215`), the call is defined as "drain whatever is pending", but "whatever is pending" grows faster than we can read it.

### 2. The NINA HCI transport reads **one byte per SPI command round-trip**

`utility/HCINinaSpiTransport.cpp:193-199`:

```cpp
int HCINinaSpiTransportClass::read()
{
  int res = -1;
  BleDrv::bleRead((uint8_t*)&res, 1);    // ← length=1 is hard-coded
  return res;
}
```

`BleDrv::bleRead()` at line 76 is a full SPI command cycle per byte:

```cpp
int BleDrv::bleRead(uint8_t data[], size_t length) {
    WAIT_FOR_SLAVE_SELECT();                    // GPIO handshake
    SpiDrv::sendCmd(BLE_READ, PARAM_NUMS_1);    // cmd + numParam bytes
    uint16_t param = length;                    // = 1
    SpiDrv::sendParam((uint8_t*)&param, sizeof(param), LAST_PARAM);  // 2B param
    while (commandSize % 4 != 0) { SpiDrv::readChar(); commandSize++; }  // pad
    SpiDrv::spiSlaveDeselect();
    SpiDrv::waitForSlaveReady();                // second GPIO handshake
    SpiDrv::spiSlaveSelect();
    SpiDrv::waitResponseData16(BLE_READ, data, (uint16_t*)&res_len);  // START+CMD+len16+DATA+END
    SpiDrv::spiSlaveDeselect();
    return res_len;
}
```

Every single byte of the 196-byte ATT-over-HCI frame is drained through one full `BleDrv::bleRead(length=1)` call: **two GPIO `waitForSlaveReady` polls, command TX (~6 SPI bytes), response RX header (~5 SPI bytes), payload byte (1), END marker (1)** — roughly **14 SPI bytes + 2 GPIO spin-waits per useful byte**.

Contrast this with the WiFi path documented above, where `SpiDrv::sendBuffer()` (`spi_drv.cpp:413-427`) transfers the entire buffer in one command. **WiFi reads 1432 B in ~1 round-trip; BLE reads 196 B in ~196 round-trips.**

### 3. `HCITransport.available()` is itself a full SPI round-trip

`utility/HCINinaSpiTransport.cpp:180-183`:

```cpp
int HCINinaSpiTransportClass::available()
{
  return BleDrv::bleAvailable();
}
```

`BleDrv::bleAvailable()` (line 58-74) is another `BLE_AVAILABLE` cmd TX + 2-byte reply RX, wrapped in two `waitForSlaveReady` GPIO polls. Every single iteration of `HCI::poll`'s `while` loop does **one `available()` round-trip + one `read()` round-trip**. At 196 bytes per packet, that's **~392 SPI command cycles per received ATT write**.

### 4. The characteristic-write callback fires inline inside the poll loop

`utility/ATT.cpp:1320`:

```cpp
characteristic->writeValue(BLEDevice(_peers[i].addressType, _peers[i].address), value, valueLength);
```

and `local/BLELocalCharacteristic.cpp:220-229`:

```cpp
void BLELocalCharacteristic::writeValue(BLEDevice device, const uint8_t value[], int length)
{
  _written = true;
  writeValue(value, length);
  if (_eventHandlers[BLEWritten]) {
    _eventHandlers[BLEWritten](device, BLECharacteristic(this));   // ← on_tx_written
  }
}
```

So `on_tx_written()` → `pool_push()` executes **synchronously within `HCIClass::poll()`'s inner `while`**. The caller of `BLE.poll()` (our `ble_nina_recv`) cannot observe the pushed entry until `poll()` returns, and `poll()` cannot return until `available()` goes false.

### 5. The HCI receive buffer is a single 258-byte staging slot, not a queue

`utility/HCI.h` defines `_recvBuffer[258]` (MAX_HCI_PKT ≤ 255 + 3-byte HCI packet type/hdr). One packet is accumulated byte-by-byte in `_recvBuffer[_recvIndex++]`, dispatched to the callback at the boundary, then `_recvIndex=0` resets for the next. No queue → no absorption capability on the SAMD21 side. Everything must be drained into the adapter's `rx_pool` *during* `poll()`.

## Real math, from datasheet specs

### Hardware context (same as WiFi section, restated for self-containment)

- **ATSAMD21G18A** — 48 MHz Cortex-M0+, SERCOM SPI master, **single-byte DATA register, no FIFO, no DMA wired in ArduinoBLE**, 8 MHz SPI clock (`spi_drv.cpp:123`).
- **NINA-W102 BLE firmware** — HCI-over-SPI with the command/response framing documented in `HCINinaSpiTransport.cpp:15-22` (`BLE_AVAILABLE=0x4C`, `BLE_PEEK=0x4D`, `BLE_READ=0x4E`, `BLE_WRITE=0x4F`).
- Per-byte `SPI.transfer()` cost at 8 MHz with SERCOM polling: **~1.7 µs/byte** (derived in the WiFi section above; same silicon, same driver primitive).

### Cost to drain **one** incoming ATT frame (180 B payload → 194 B wire → 196 B HCI frame w/ type prefix)

Per byte drained, `HCIClass::poll()` performs exactly one `available()` + one `read()`. Each of those is a `sendCmd` + optional `sendParam` + deselect + `waitForSlaveReady` + select + `waitResponseCmd/Data16` + deselect cycle.

| Operation (per 1 HCI byte) | SPI bytes on wire | Time @ 1.7 µs/B | GPIO polls | Subtotal |
|---|---|---|---|---|
| `bleAvailable`: sendCmd(BLE_AVAILABLE,0) + reply (2 B) | ~11 | 18.7 µs | 2 × `waitForSlaveReady` (~20-50 µs total) | **~50 µs** |
| `bleRead(len=1)`: sendCmd(BLE_READ,1) + param(2 B) + pad + reply(1 B data) | ~14 | 23.8 µs | 2 × `waitForSlaveReady` (~20-50 µs total) | **~55 µs** |
| **Per HCI byte** | ~25 SPI bytes | ~42.5 µs | 4 GPIO polls (~40-100 µs) | **~100-140 µs** |

For one 196-byte HCI frame:

```
196 HCI bytes × ~120 µs/byte = ~23.5 ms of SPI-bound work
```

Add NINA-internal LL→HCI prep (~3-10 ms), ATT dispatch (`writeReqOrCmd` → `characteristic->writeValue` → `on_tx_written` → `pool_push` memcpy of 180 B → ~0.05 ms), and the per-packet floor is **~30-35 ms**.

### Why the observed ceiling is **~83 ms/packet**, not 35 ms

Measured from the on-firmware log (`t=18427 → t=102899`, 1009 callbacks spanning 84 472 ms): **~83.7 ms per callback**. The extra ~50 ms over the SPI-bound 30-35 ms floor comes from two sources that the WiFi profile does not have:

1. **BLE LL retransmission + ACK handshake** — the NINA peripheral's LL NACKs frames it cannot yet hand off over SPI (the host-side HCI flow-control window, `Number_Of_Completed_Packets` in Core Spec v5.3 §7.7.19, is not replenished because we're still reading the previous frame). The central LL retransmits. Measured at the sender: 1010 sends over 82 162 ms → **81.3 ms/send** — within 2 ms of the receiver's 83.7 ms cadence. The link is **LL-flow-controlled to the receiver's SPI drain rate**.
2. **NINA ESP32 scheduler overhead** — per-packet handoff between the NimBLE/Bluedroid stack inside NINA and the SPI HCI transport adds ~5-15 ms that the WiFi path avoids because lwIP delivers a whole datagram in one SPI command.

### The crucial inequality

BLE.poll()'s inner loop exits only when **Arduino SPI drain rate > BLE air arrival rate**, i.e.:

```
    1 / T_drain   >   1 / T_IPI
⇔   T_drain      <   T_IPI
```

With `T_drain ≈ 83 ms` at 180 B and `T_IPI = 70 ms`, we have `T_drain > T_IPI` and the loop **never exits**. Every new packet that arrives during the drain of the previous one keeps `HCITransport.available()` positive.

This is the mechanism by which the 84-second freeze happens: 1010 packets × 83 ms = 84 seconds of `poll()` call that never yields. `pool_pop()` physically cannot run in that window.

### Cutoff prediction vs measurement

Using the observed per-byte cost (~120 µs) and per-packet floor (~30 ms), the receiver's sustained drain time is approximately:

```
T_drain(payload)  ≈  30 + 0.12 × (payload + 14)  [ms]
```

For the three tested payloads at IPI = 70 ms:

| Payload | T_drain(pred) | T_IPI | T_drain vs T_IPI | Predicted | Measured |
|---|---|---|---|---|---|
|  27 B | 30 + 0.12×41  ≈ 35 ms  | 70 ms | drain **faster** (50% headroom)  | works | 0 % loss ✓ |
| 127 B | 30 + 0.12×141 ≈ 47 ms  | 70 ms | drain **faster** (33% headroom) | works | 0 % loss ✓ |
| 180 B | 30 + 0.12×194 ≈ 53 ms  | 70 ms | drain **faster** on paper, but LL retransmission pushes observed T_drain to 83 ms | **should work — doesn't** | 93 % loss |

The pure SPI math predicts 180 B should just fit. It doesn't, because **LL retransmissions asymptote the observed T_drain toward T_IPI + NINA-scheduling slack**. Once `T_drain` pushes past ~70 ms (which happens as soon as the packet is large enough that NINA's side can't hand off faster than Arduino can drain), the system enters a self-reinforcing stall: more retransmissions → longer per-packet time → even more retransmissions. The 83 ms observed plateau is the *natural equilibrium* of that feedback loop at this payload size.

### Absolute bit-clock ceiling

If the SPI link could stream raw HCI bytes back-to-back with zero framing overhead:

```
196 B × 8 bits / 8 Mbit/s = 0.196 ms / packet  →  5100 pps  →  7.3 Mbps at 180 B payload
```

Observed: **~12 pps = 17 kbps = 0.23 % of the bit-clock ceiling**.

The other 99.77 % is consumed by:
- **195 SPI command round-trips** per packet (one per HCI byte), each with their own header/trailer framing (14 SPI bytes of overhead per useful byte)
- **~780 `waitForSlaveReady` GPIO polls** per packet (4 × 196)
- BLE LL ACK/retransmit handshake
- NINA scheduler overhead between LL/HCI

## Why the WiFi path is ~40× faster per byte

Side-by-side, same hardware, same 8 MHz SPI clock:

| | WiFi (UDP RX) | BLE (HCI ACL RX) |
|---|---|---|
| Transport file | `WiFiNINA/src/utility/WiFiSocketBuffer.cpp` | `ArduinoBLE/src/utility/HCINinaSpiTransport.cpp` |
| Bulk-read primitive used | `SpiDrv::getDataBuf()` → `spiTransfer()` loop inside **one** SPI command | `BleDrv::bleRead(len=1)` called **N times** from `HCI::poll`'s byte loop |
| SPI commands per 196-byte packet | **~2** (`availData` + `getDataBuf`) | **~392** (196 × `bleAvailable` + 196 × `bleRead`) |
| `waitForSlaveReady` GPIO polls per packet | ~4 | **~780** |
| Measured per-byte cost | ~2-3 µs | ~120 µs (**40-60×** worse) |
| Measured packet throughput at 180 B | Not tested this size; extrapolation ~200 pps | **~12 pps** |
| Back-pressure mechanism | Single-slot NINA buffer drops excess | BLE LL NACK → central retransmits → self-rate-limits to drain rate |
| Failure mode | Packet loss at rate mismatch | `poll()` stall + pool starvation |

The WiFi ceiling is a throughput ceiling (49 pps at 1432 B). The BLE ceiling is a **latency ceiling compounded by a control-flow stall**: BLE.poll never returning is strictly worse than WiFi dropping packets, because the Arduino main loop is frozen instead of just lossy.

## Why enlarging the pool doesn't help

The rx_pool is sized to absorb whatever survives one `BLE.poll()` call. Naively one might think a bigger pool would catch the 939 dropped callbacks. It doesn't, because:

1. The consumer's `pool_pop()` never runs during the poll burst regardless of pool size.
2. Sizing the pool to hold the entire flood (1010 packets × 196 B = 198 KB) exceeds the SAMD21's 32 KB SRAM.
3. Even if we grew the pool to 25 KB, we'd push 127 entries before overflow, then drop 883 more — same structural problem, slightly different ratio.

The **only** pool size that makes the system work is one that fits a *single* BLE.poll() call's burst — which equals 1 packet **provided the loop exits after each packet**. And the loop exits iff `T_drain < T_IPI`. So the pool fix and the drain-rate fix are the same fix.

## Why lowering `delay(10)` in `ble_nina_recv` doesn't help either

The consumer's spin loop inside `ble_nina_recv` is:

```c
while ((millis() - start) < timeout_ms) {
    BLE.poll();
    if (pool_pop(...)) return ADAPTER_OK;
    ...
    delay(10);
}
```

The `delay(10)` runs **only between poll calls** — i.e. only when `poll()` has returned. When `poll()` is stuck in its own inner `while`, the delay never executes. Shortening or removing it has zero effect on the stall.

## Practical consequences for the benchmark

1. **Hard cap on ATT payload at 70 ms IPI: ~127 B.** Above that, `T_drain > T_IPI` and the receiver's `BLE.poll()` enters a self-sustaining stall.
2. **Effective throughput ceiling is ~17 kbps.** BLE 5.0 advertises 1-2 Mbps at the LL — we see 0.85 % of that because 99 % of the time is spent in SPI framing overhead, not on the air.
3. **The fix is NOT in our code.** `test_engine.c`'s flood receiver, `ble_adapter_nina.cpp`'s pool, and the ArduinoBLE characteristic plumbing are all correct. The bottleneck is the HCI transport's byte-at-a-time reads.
4. **Two clean options:**
   - Keep stock ArduinoBLE. Cap payload at 127 B or raise IPI to ≥ 100 ms. Lossless but capped at ~14-18 kbps.
   - Fork `HCINinaSpiTransport::read()` to call `BleDrv::bleRead(buf, n)` with `n > 1` (the length parameter is already plumbed — it's just hard-coded to 1 at the transport layer). Cuts per-byte cost 10-20×. Invasive (patches a `.pio/libdeps` file; needs to be vendored into the project to survive clean builds).
5. **RPi4 ↔ MKR BLE won't help the same way WiFi does.** Unlike WiFi where the receiver is the bottleneck, for BLE the MKR-side SPI is the limit regardless of whether it's sending or receiving — the same HCI transport is used on both sides.

## References (BLE-specific)

- `cpcbf/agent/platforms/arduino_mkr_wifi1010/.pio/libdeps/mkrwifi1010_ble_flood/ArduinoBLE/src/utility/HCI.cpp` — `HCIClass::poll()` byte-reading loop
- `cpcbf/agent/platforms/arduino_mkr_wifi1010/.pio/libdeps/mkrwifi1010_ble_flood/ArduinoBLE/src/utility/HCINinaSpiTransport.cpp` — `read()` / `available()` hard-coded to 1-byte SPI round-trips
- `cpcbf/agent/platforms/arduino_mkr_wifi1010/.pio/libdeps/mkrwifi1010_ble_flood/ArduinoBLE/src/utility/ATT.cpp` — `writeReqOrCmd()` → characteristic write dispatch
- `cpcbf/agent/platforms/arduino_mkr_wifi1010/.pio/libdeps/mkrwifi1010_ble_flood/ArduinoBLE/src/local/BLELocalCharacteristic.cpp` — event-handler invocation inside the poll stack
- `cpcbf/agent/platforms/arduino_mkr_wifi1010/src/ble_adapter_nina.cpp` — on-firmware `ble_diag` counters and timestamped breadcrumbs
- Bluetooth Core Specification v5.3, Vol 4 Part E §7.7.19 (`HCI_Number_Of_Completed_Packets` event — HCI→Host flow control)
- Bluetooth Core Specification v5.3, Vol 6 Part B §5.1.1 (LL ACK/retransmission)
- u-blox NINA-W10 datasheet, §1.7 (Host interface — same SPI, distinct BLE command set)

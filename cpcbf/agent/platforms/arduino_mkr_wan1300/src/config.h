/*
 * CPCBF — Arduino MKR WAN 1300 Configuration (LoRa P2P)
 * SAMD21 Cortex-M0+: 256 KB flash, 32 KB SRAM.
 * Radio: Murata CMWX1ZZABZ-078 (SX1276 inside), driven by Sandeep Mistry LoRa lib.
 */
#ifndef CPCBF_WAN1300_CONFIG_H
#define CPCBF_WAN1300_CONFIG_H

/* Max payload set via build flag (-DBENCH_MAX_PAYLOAD=200)
 * SX1276 MTU at SF7/BW125 is 222 B; 200 B + 14 B bench header leaves
 * a small safety margin for library-internal framing. */

#ifndef MAX_RESULTS
#define MAX_RESULTS 500
#endif

#define SERIAL_BAUD 115200

/* LoRa radio defaults (EU868). Switch to 915E6 for US915.
 * Keep both boards on identical parameters — SF/BW/sync-word mismatch
 * silently drops packets on reception. */
#ifndef CPCBF_LORA_REGION_US
#define LORA_FREQUENCY     868E6   /* Hz — EU868 */
#else
#define LORA_FREQUENCY     915E6   /* Hz — US915 */
#endif

#define LORA_SF            7       /* spreading factor 7..12 (7 = fastest) */
#define LORA_BW            125E3   /* bandwidth Hz (125 kHz standard) */
#define LORA_CR            5       /* coding rate denominator: 5 = 4/5 */
#define LORA_TX_POWER_DBM  14      /* +14 dBm (PA_BOOST on MKR WAN 1300) */
#define LORA_PREAMBLE      8       /* preamble length */
#define LORA_SYNC_WORD     0x12    /* private network (0x34 = public LoRaWAN) */

#endif /* CPCBF_WAN1300_CONFIG_H */

/*
 * CPCBF — Cross-Platform Communication Benchmarking Framework
 * Platform Hardware Abstraction Layer
 */
#ifndef CPCBF_PLATFORM_HAL_H
#define CPCBF_PLATFORM_HAL_H

#include <stdint.h>

/* Returns monotonic timestamp in microseconds (wraps every ~71 minutes). */
uint32_t platform_timestamp_us(void);

/* Log a message to stderr with a timestamp prefix. */
void platform_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* Sleep for the given number of milliseconds. */
void platform_sleep_ms(uint32_t ms);

/* Disable the radio via rfkill or equivalent. Returns 0 on success. */
int platform_radio_disable(const char *subsystem);

/* Enable the radio via rfkill or equivalent. Returns 0 on success. */
int platform_radio_enable(const char *subsystem);

/* Check if the radio subsystem is active. Returns 1 if active, 0 if blocked, <0 on error. */
int platform_radio_is_active(const char *subsystem);

/* Returns a static string identifying the platform (e.g. "rpi4"). */
const char *platform_name(void);

#endif /* CPCBF_PLATFORM_HAL_H */

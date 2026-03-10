/*
 * CPCBF — RPi4 Platform HAL Implementation
 */
#include "platform_hal.h"
#include <time.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

uint32_t platform_timestamp_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000000ULL + ts.tv_nsec / 1000ULL);
}

void platform_log(const char *fmt, ...)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    fprintf(stderr, "[%ld.%06ld] ", ts.tv_sec, ts.tv_nsec / 1000);

    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);

    fprintf(stderr, "\n");
}

void platform_sleep_ms(uint32_t ms)
{
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}

void platform_sleep_us(uint32_t us)
{
    struct timespec ts;
    ts.tv_sec = us / 1000000;
    ts.tv_nsec = (us % 1000000) * 1000L;
    nanosleep(&ts, NULL);
}

int platform_radio_disable(const char *subsystem)
{
    char cmd[128];
    snprintf(cmd, sizeof(cmd), "rfkill block %s 1>&2", subsystem);
    return system(cmd);
}

int platform_radio_enable(const char *subsystem)
{
    char cmd[128];
    snprintf(cmd, sizeof(cmd), "rfkill unblock %s 1>&2", subsystem);
    return system(cmd);
}

int platform_radio_is_active(const char *subsystem)
{
    char cmd[256];
    snprintf(cmd, sizeof(cmd), "rfkill list %s 2>/dev/null", subsystem);
    FILE *fp = popen(cmd, "r");
    if (!fp)
        return -1;

    char line[256];
    int blocked = 0;
    while (fgets(line, sizeof(line), fp)) {
        if (strstr(line, "Soft blocked: yes") || strstr(line, "Hard blocked: yes"))
            blocked = 1;
    }
    pclose(fp);

    return blocked ? 0 : 1;
}

const char *platform_name(void)
{
    return "rpi4";
}

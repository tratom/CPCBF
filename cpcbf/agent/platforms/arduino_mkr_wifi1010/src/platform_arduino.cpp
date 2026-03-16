/*
 * CPCBF — Arduino MKR WiFi 1010 Platform HAL
 * Implements platform_hal.h for SAMD21 + WiFiNINA.
 */
#include <Arduino.h>
#include <stdarg.h>

extern "C" {
#include "platform_hal.h"
}

extern "C" uint32_t platform_timestamp_us(void)
{
    return micros();
}

extern "C" void platform_log(const char *fmt, ...)
{
    /* Prefix with '# ' so the serial relay can route to stderr */
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);

    Serial.print("# [");
    Serial.print(millis());
    Serial.print("] ");
    Serial.println(buf);
}

extern "C" void platform_sleep_ms(uint32_t ms)
{
    delay(ms);
}

extern "C" void platform_sleep_us(uint32_t us)
{
    delayMicroseconds(us);
}

extern "C" int platform_radio_disable(const char *subsystem)
{
    (void)subsystem;
    /* NINA module has WiFi only — no rfkill equivalent */
    return 0;
}

extern "C" int platform_radio_enable(const char *subsystem)
{
    (void)subsystem;
    return 0;
}

extern "C" int platform_radio_is_active(const char *subsystem)
{
    (void)subsystem;
    /* WiFi is always the only radio; report BT as inactive */
    if (strcmp(subsystem, "bluetooth") == 0)
        return 0;
    return 1;  /* WiFi active */
}

extern "C" const char *platform_name(void)
{
    return "mkr_wifi_1010";
}

/*
 * CPCBF — Arduino MKR WiFi 1010 Platform HAL
 * Implements platform_hal.h for SAMD21.
 */
#include <Arduino.h>
#ifdef CPCBF_PROTOCOL_WIFI
#include <WiFiNINA.h>
#endif
#ifdef CPCBF_PROTOCOL_BLE
#include <ArduinoBLE.h>
#endif
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
#ifdef CPCBF_PROTOCOL_WIFI
    if (strcmp(subsystem, "wifi") == 0 || strcmp(subsystem, "all") == 0)
        WiFi.end();
#endif
#ifdef CPCBF_PROTOCOL_BLE
    if (strcmp(subsystem, "bluetooth") == 0 || strcmp(subsystem, "all") == 0)
        BLE.end();
#endif
    return 0;
}

extern "C" int platform_radio_enable(const char *subsystem)
{
    (void)subsystem;
    /* Radios are enabled via adapter init, not this function */
    return 0;
}

extern "C" int platform_radio_is_active(const char *subsystem)
{
#ifdef CPCBF_PROTOCOL_WIFI
    if (strcmp(subsystem, "wifi") == 0)
        return (WiFi.status() != WL_NO_MODULE && WiFi.status() != WL_NO_SHIELD) ? 1 : 0;
#endif
#ifdef CPCBF_PROTOCOL_BLE
    if (strcmp(subsystem, "bluetooth") == 0)
        return BLE.connected() ? 1 : 0;
#endif
    return 0;
}

extern "C" const char *platform_name(void)
{
    return "mkr_wifi_1010";
}

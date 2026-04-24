/*
 * CPCBF — Arduino MKR WAN 1300 Platform HAL
 * Implements platform_hal.h for SAMD21 + LoRa.
 */
#include <Arduino.h>
#ifdef CPCBF_PROTOCOL_LORA
#include <LoRa.h>
#endif
#include <stdarg.h>
#include <string.h>

extern "C" {
#include "platform_hal.h"
}

#ifdef CPCBF_PROTOCOL_LORA
/* Tracked by the LoRa adapter — declared here so platform_radio_is_active
 * can answer RADIO_STATUS without pulling in adapter internals. */
extern "C" int lora_adapter_is_active(void);
#endif

extern "C" uint32_t platform_timestamp_us(void)
{
    return micros();
}

extern "C" void platform_log(const char *fmt, ...)
{
    /* Prefix with '# ' so the serial relay routes to stderr */
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
    if (us >= 1000) {
        delay(us / 1000);
        us %= 1000;
    }
    if (us > 0)
        delayMicroseconds(us);
}

extern "C" int platform_radio_disable(const char *subsystem)
{
#ifdef CPCBF_PROTOCOL_LORA
    if (strcmp(subsystem, "lora") == 0 || strcmp(subsystem, "all") == 0)
        LoRa.end();
#else
    (void)subsystem;
#endif
    return 0;
}

extern "C" int platform_radio_enable(const char *subsystem)
{
    (void)subsystem;
    /* Radios are (re)enabled via adapter init, not this function */
    return 0;
}

extern "C" int platform_radio_is_active(const char *subsystem)
{
#ifdef CPCBF_PROTOCOL_LORA
    if (strcmp(subsystem, "lora") == 0)
        return lora_adapter_is_active();
#else
    (void)subsystem;
#endif
    return 0;
}

extern "C" const char *platform_name(void)
{
    return "mkr_wan_1300";
}

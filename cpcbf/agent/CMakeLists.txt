cmake_minimum_required(VERSION 3.13)
project(cpcbf_agent C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
add_compile_options(-Wall -Wextra -Wpedantic)

option(BUILD_RPI4 "Build the RPi4 agent binary" OFF)

# ---- Platform-independent common library ----
add_library(cpcbf_common STATIC
    common/benchmark_packet.c
    common/test_engine.c
)
target_include_directories(cpcbf_common PUBLIC common)

# ---- Host-side unit tests ----
add_executable(test_packet_codec
    tests/test_packet_codec.c
)
target_link_libraries(test_packet_codec PRIVATE cpcbf_common)

# ---- RPi4 agent (conditional) ----
if(BUILD_RPI4)
    add_library(cpcbf_cjson STATIC
        common/cjson/cJSON.c
    )
    target_include_directories(cpcbf_cjson PUBLIC common/cjson)

    add_executable(cpcbf_agent
        platforms/rpi4/main.c
        platforms/rpi4/platform_rpi4.c
        platforms/rpi4/wifi_setup.c
        platforms/rpi4/ble_setup.c
        adapters/wifi_adapter.c
        adapters/ble_adapter.c
    )
    target_link_libraries(cpcbf_agent PRIVATE cpcbf_common cpcbf_cjson bluetooth)
    target_include_directories(cpcbf_agent PRIVATE
        common
        platforms/rpi4
        adapters
    )
endif()

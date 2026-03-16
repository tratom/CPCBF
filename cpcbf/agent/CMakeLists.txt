cmake_minimum_required(VERSION 3.13)
project(cpcbf_agent C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_compile_options(-Wall -Wextra -Wpedantic)

option(BUILD_RPI4 "Build the RPi4 agent binary" OFF)

# ---- Platform-independent common library (pure C) ----
add_library(cpcbf_common STATIC
    common/benchmark_packet.c
    common/test_engine.c
    common/sync_barrier.c
)
target_include_directories(cpcbf_common PUBLIC common)

# ---- Host-side unit tests ----
add_executable(test_packet_codec
    tests/test_packet_codec.c
)
target_link_libraries(test_packet_codec PRIVATE cpcbf_common)

# ---- ArduinoJson (header-only, fetched via FetchContent) ----
include(FetchContent)
FetchContent_Declare(
    ArduinoJson
    GIT_REPOSITORY https://github.com/bblanchon/ArduinoJson.git
    GIT_TAG        v7.4.1
)
FetchContent_MakeAvailable(ArduinoJson)

# ---- RPi4 agent (conditional) ----
if(BUILD_RPI4)
    add_executable(cpcbf_agent
        platforms/rpi4/main.cpp
        platforms/rpi4/platform_rpi4.c
        platforms/rpi4/wifi_setup.c
        platforms/rpi4/ble_setup.c
        adapters/wifi_adapter.c
        adapters/ble_adapter.c
    )
    target_link_libraries(cpcbf_agent PRIVATE cpcbf_common ArduinoJson bluetooth)
    target_include_directories(cpcbf_agent PRIVATE
        common
        platforms/rpi4
        adapters
    )
endif()

# emap-bridge links mockturtle, which the rest of the mapper does not need, so
# it is a standalone project rather than part of mapper/:
#
#   cmake -S mapper/tools/emap-bridge -B <build> -DMOCKTURTLE_DIR=<checkout>
#   cmake --build <build> --target emap-bridge
#
# <checkout> is lsils/mockturtle at the commit in patches/mockturtle-emap.patch
# with that patch applied.
cmake_minimum_required(VERSION 3.20)
project(emap-bridge CXX)

set(MOCKTURTLE_DIR "" CACHE PATH "mockturtle checkout, patched")
if(NOT EXISTS "${MOCKTURTLE_DIR}/include/mockturtle/mockturtle.hpp")
  message(FATAL_ERROR "MOCKTURTLE_DIR must point at a mockturtle checkout")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(MOCKTURTLE_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
add_subdirectory(${MOCKTURTLE_DIR} mockturtle EXCLUDE_FROM_ALL)

add_executable(emap-bridge emap_bridge.cpp)
target_link_libraries(emap-bridge PRIVATE mockturtle)

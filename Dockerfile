# syntax=docker/dockerfile:1
#
# The circt-abc image: stock OpenROAD-flow-scripts (OpenROAD, Yosys and the
# asap7 platform, from the pinned ORFS image) plus everything the flow
# builds itself:
#
#   circt-verilog, circt-opt  CIRCT at third_party/CIRCT_REV + patches/circt
#   circt-abc-opt             the mapper (mapper/)
#   yosys-abc                 ABC at patches/abc/BASE + patches/abc
#   emap-bridge               mapper/tools/emap-bridge, against mockturtle at
#                             the commit in patches/mockturtle-emap.patch
#   sv2v                      zachjs/sv2v release v0.0.11
#
# Build from an exported tree (it is the whole build context):
#
#   docker build -t circt-abc .
#
# Binaries sit where the scripts look by default (third_party/circt/build/bin,
# mapper/build/bin, third_party/yosys/build) under /opt/circt-abc, so the flow
# runs unchanged:
#
#   docker run --rm -v $PWD/out:/out circt-abc \
#     python3 -m circt_flow ../designs/ibex/asap7_flow.yaml /out/ibex --pnr
#
# ORFS_HOME and SV2V make the P&R stage and the sv2v step run in place
# rather than in containers of their own.

ARG ORFS_IMAGE=openroad/orfs:26Q3-277-gf9ec54a6d

# ---------------------------------------------------------------------------
FROM ${ORFS_IMAGE} AS build
ARG JOBS=16
RUN apt-get update \
 && apt-get install -y --no-install-recommends ninja-build \
 && rm -rf /var/lib/apt/lists/*
# Patches are applied with git am; author and dates come from the patches.
ENV GIT_COMMITTER_NAME=circt-abc GIT_COMMITTER_EMAIL=circt-abc@localhost
WORKDIR /src
COPY third_party/CIRCT_REV third_party/CIRCT_REV
COPY patches patches
COPY scripts/build-circt-from-source.sh scripts/build-mapper.sh scripts/

# CIRCT. A shallow checkout of the base; build-circt-from-source.sh applies
# patches/circt, fetches the llvm submodule and builds LLVM/MLIR and CIRCT.
RUN rev=$(tr -d '[:space:]' < third_party/CIRCT_REV) \
 && git init -q third_party/circt \
 && git -C third_party/circt remote add origin https://github.com/llvm/circt.git \
 && git -C third_party/circt fetch -q --depth 1 origin "$rev" \
 && git -C third_party/circt checkout -q --detach FETCH_HEAD
RUN JOBS=$JOBS scripts/build-circt-from-source.sh

COPY mapper mapper
RUN JOBS=$JOBS scripts/build-mapper.sh

# ABC, as yosys-abc.
RUN rev=$(awk '/^# Base:/ {print $3; exit}' patches/abc/BASE) \
 && git init -q abc \
 && git -C abc fetch -q --depth 1 https://github.com/YosysHQ/abc.git "$rev" \
 && git -C abc checkout -q --detach FETCH_HEAD \
 && git -C abc am -q --committer-date-is-author-date /src/patches/abc/0*.patch \
 && make -C abc -j"$JOBS" ABC_USE_NO_READLINE=1 abc

# emap-bridge against patched mockturtle.
RUN rev=$(sed -n '1s/.*mockturtle @ \([0-9a-f]\{40\}\).*/\1/p' patches/mockturtle-emap.patch) \
 && git init -q mockturtle \
 && git -C mockturtle fetch -q --depth 1 https://github.com/lsils/mockturtle.git "$rev" \
 && git -C mockturtle checkout -q --detach FETCH_HEAD \
 && git -C mockturtle apply /src/patches/mockturtle-emap.patch \
 && cmake -S mapper/tools/emap-bridge -B emap-build -G Ninja \
      -DMOCKTURTLE_DIR=/src/mockturtle \
 && cmake --build emap-build --target emap-bridge --parallel "$JOBS"

RUN wget -q https://github.com/zachjs/sv2v/releases/download/v0.0.11/sv2v-Linux.zip \
      -O /tmp/sv2v.zip \
 && unzip -q /tmp/sv2v.zip -d /tmp/sv2v \
 && install -m 755 /tmp/sv2v/sv2v-Linux/sv2v /src/sv2v

# ---------------------------------------------------------------------------
FROM ${ORFS_IMAGE}
ARG ORFS_IMAGE
ENV CIRCT_ABC=/opt/circt-abc
WORKDIR ${CIRCT_ABC}

COPY --from=build /src/third_party/circt/build/bin/circt-verilog \
                  /src/third_party/circt/build/bin/circt-opt \
                  third_party/circt/build/bin/
COPY --from=build /src/mapper/build/bin/circt-abc-opt \
                  /src/emap-build/emap-bridge \
                  mapper/build/bin/
COPY --from=build /src/abc/abc third_party/yosys/build/yosys-abc
COPY --from=build /src/sv2v /usr/local/bin/sv2v
# ORFS's Yosys (it has read_slang built in) serves the CEC and miter scripts.
RUN ln -s /OpenROAD-flow-scripts/tools/install/yosys/bin/yosys \
          third_party/yosys/build/yosys

COPY third_party/CIRCT_REV third_party/CIRCT_REV
COPY scripts scripts
COPY designs designs
COPY profiles profiles
COPY patches patches

ENV ORFS_HOME=/OpenROAD-flow-scripts \
    ORFS_IMAGE=${ORFS_IMAGE} \
    SV2V=/usr/local/bin/sv2v \
    CIRCT_ABC_CACHE=/opt/circt-abc/cache
# Extract and merge the asap7 liberties now, so a run needs no setup; the
# cache stays writable for RTL sources fetched by the front end.
RUN cd scripts && python3 -m circt_flow.profile ../profiles/asap7.yaml \
 && chmod -R a+rwX ${CIRCT_ABC_CACHE}
WORKDIR ${CIRCT_ABC}/scripts

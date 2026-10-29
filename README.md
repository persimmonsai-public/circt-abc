# circt-abc

A synthesis flow from SystemVerilog to a mapped standard-cell netlist built on
[CIRCT](https://github.com/llvm/circt) and [ABC](https://github.com/YosysHQ/abc),
with place and route on stock
[OpenROAD-flow-scripts](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts)
(ORFS). The public example targets the open ASAP7 library.

The flow keeps the design in CIRCT's hw/comb/seq dialects from elaboration to
the mapped netlist. Registers and clock gates are mapped by passes in
`mapper/` that read their cells from the liberty (optional passes do the same
for adder and multiplexer structures); the remaining combinational logic goes
to ABC as an AIG and comes back mapped.

## Quick start

Build the image (OpenROAD, ORFS and the asap7 platform come from the pinned
ORFS base image; CIRCT, the mapper, ABC, emap-bridge and sv2v are built from
this tree):

```bash
docker build -t circt-abc .
```

Run ibex on ASAP7, synthesis and place and route:

```bash
mkdir -p out
docker run --rm -u $(id -u):$(id -g) -e HOME=/tmp -v $PWD/out:/out circt-abc \
  python3 -m circt_flow ../designs/ibex/asap7_flow.yaml /out/ibex --pnr
```

The RTL is fetched at the commit pinned in `designs/ibex/manifest.yaml`. The
mapped netlist is `out/ibex/1_synth/ibex_top_openroad.v`; ORFS's results are
under `out/ibex/pnr/`, summarised in `out/ibex/pnr/pnr_metrics.json`.

## Layout

| path | what |
| :--- | :--- |
| `mapper/` | the MLIR passes (`circt-abc-opt`), their lit tests, and `emap-bridge` |
| `scripts/circt_flow/` | the flow driver: front end, stages, ORFS P&R stage |
| `designs/` | design manifests (source, files, defines) and flow configs |
| `profiles/` | library profiles: liberties, ICG and tie cells, dont_use |
| `patches/` | patches to CIRCT, ABC and mockturtle (see `patches/README`) |
| `scripts/verify/` | equivalence checks: register-mapper miter, top-level miter |
| `Dockerfile` | the circt-abc image |

## Building without Docker

```bash
scripts/build-circt-from-source.sh   # CIRCT at third_party/CIRCT_REV + patches/circt
scripts/build-mapper.sh              # circt-abc-opt
```

`third_party.lock` lists every pinned dependency.

## Verification

`scripts/verify/register_mapper_miter.sh` proves the register mapping of a
design equivalent to its RTL registers with a bounded sequential miter; every
SUCCESS must be paired with a deliberately broken gate that FAILs.
`scripts/design_level_cec.sh` compares two mapped netlists.

## Status

Early. ibex runs end to end on ASAP7; CVA6 manifests and an ASAP7 baseline
against ORFS's own synthesis are next.

## License

Apache-2.0 WITH LLVM-exception (see `LICENSE`). Patches to other projects are
under those projects' licenses (see `patches/README`).

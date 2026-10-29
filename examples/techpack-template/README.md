# Tech pack template

A complete tech pack (doc/techpacks.md) on an invented cell library, to
copy as the start of your own. It runs as it is:

```bash
cd scripts
python3 -m circt_flow.techpack check ../examples/techpack-template/techpack.yaml
python3 -m circt_flow ../examples/techpack-template/designs/ibex/flow.yaml ~/runs/tpl --pnr
```

| File | What it is |
|---|---|
| `techpack.yaml` | The profile: liberties, ICG and tie cells, dont_use, the audit's terms, flow defaults, P&R |
| `lib/template.lib` | The invented liberty: 21 cells, simple NLDM tables, no foundry data |
| `lib/make_lib.py` | Writes `template.lib` |
| `designs/ibex/flow.yaml` | ibex_top (circt-abc's `designs/ibex/manifest.yaml`) on this library |
| `pnr/run.py` | The P&R command interface, as a stand-in that only counts cells |

To make it yours: copy the directory outside circt-abc and into a private
repository; set `name:`; point `liberties:` at your library (`${VAR}` for
a PDK installed elsewhere); set `icg`, `tie`, `dont_use` and `time_unit`;
list your foundry's and library's names in `deny_terms`; replace
`pnr.command` with your P&R or with `orfs_platform_dir`; then put the
directory that holds it on `CIRCT_ABC_TECH_PATH` and run `techpack check`.

#!/usr/bin/env python3
"""Build the asap7-mvt example's liberties from the ORFS asap7 platform.

    make_libs.py <lib_dir> [-W <leak weight>] [--image <orfs image>]

ASAP7 (the open predictive 7 nm PDK, BSD-3) ships each threshold flavour
(RVT, LVT, SLVT; cell suffixes _R, _L, _SL) at three corners, TT 0.7 V
25 C, SS 0.63 V 100 C and FF 0.77 V 0 C, as five NLDM files per flavour
and corner (AO, INVBUF, OA, SEQ, SIMPLE). They come from the ORFS image's
platform directory: in place under $ORFS_HOME (the circt-abc image), else
copied out of the image with docker. Writes, into <lib_dir>:

  asap7_<rvt|lvt|slvt>_<tt|ss|ff>.lib
      one flavour at one corner: the five files merged, then slimmed
      (scripts/mvt/slim_liberty.py: header, templates, area,
      cell_leakage_power -- the mean over the per-state leakage_power
      groups, which is all ASAP7 gives -- pins and NLDM timing). Areas are
      real.
  asap7_mvt_tt_abc.lib
      the three flavours at tt merged and PRICED for the mapper
      (scripts/mvt/make_abc_lib.py: area := S*(area + W*leakage), W in
      um^2 per pW, the library's leakage unit), with tt timing and the
      leakage of --leakage-corner (default ss: at 100 C it is ASAP7's
      leakiest corner; ff is 0 C). The platform's dont_use cells are left
      out. Its areas are prices, never areas.

Each file is built only when missing; the priced library also when its -W
or --leakage-corner changed (asap7_mvt_tt_abc.args records them). Generated
files are not committed (.gitignore): the platform is the source.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from circt_flow.profile import _extract_orfs, merge_liberties  # noqa: E402
from circt_flow.orfs_pnr import DEFAULT_IMAGE  # noqa: E402

FLAVOURS = {"rvt": "RVT", "lvt": "LVT", "slvt": "SLVT"}
CORNERS = {"tt": "TT", "ss": "SS", "ff": "FF"}
GROUPS = [("AO", "211120", True), ("INVBUF", "220122", True),
          ("OA", "211120", True), ("SEQ", "220123", False),
          ("SIMPLE", "211120", True)]
# The asap7 platform's DONT_USE_CELLS (profiles/asap7.yaml) except ICG*:
# integrated clock gates are dont_use as logic, but the flow instantiates
# them for clock gating, so the mapping library keeps them (the pack's
# dont_use still keeps them out of logic).
DONT_USE = ["*x1p*_ASAP7*", "*xp*_ASAP7*", "SDF*"]


def platform_files(vt: str, corner: str):
    for g, date, gz in GROUPS:
        yield (f"lib/NLDM/asap7sc7p5t_{g}_{vt}_{corner}_nldm_{date}.lib"
               + (".gz" if gz else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("lib_dir", type=Path)
    ap.add_argument("-W", "--leak-weight", default="0.00001",
                    help="leakage price, um^2 per pW (default 0.00001)")
    ap.add_argument("--leakage-corner", default="ss", choices=CORNERS,
                    help="the corner whose leakage is priced (default ss)")
    ap.add_argument("--image", default=os.environ.get("ORFS_IMAGE",
                                                      DEFAULT_IMAGE))
    a = ap.parse_args()
    out = a.lib_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    py = [sys.executable, "-I"]
    slim = str(ROOT / "scripts/mvt/slim_liberty.py")
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        tmp = Path(tmp)
        for v, V in FLAVOURS.items():
            for c, C in CORNERS.items():
                lib = out / f"asap7_{v}_{c}.lib"
                if lib.exists():
                    continue
                files = _extract_orfs(a.image, "asap7",
                                      list(platform_files(V, C)), tmp / v / c)
                merged = tmp / f"{v}_{c}.lib"
                merge_liberties(files, merged)
                subprocess.run([*py, slim, str(merged), str(lib),
                                "--rename", f"asap7_{v}_{c}"], check=True,
                               stdout=subprocess.DEVNULL)
                print(f"[asap7-mvt] {lib.name}")
    prim = out / "asap7_mvt_tt_abc.lib"
    stamp = out / "asap7_mvt_tt_abc.args"
    want = f"W {a.leak_weight} leakage {a.leakage_corner}\n"
    if prim.exists() and stamp.exists() and stamp.read_text() == want:
        return
    cmd = [*py, str(ROOT / "scripts/mvt/make_abc_lib.py"), str(prim),
           *(str(out / f"asap7_{v}_tt.lib") for v in FLAVOURS),
           "-W", a.leak_weight]
    for p in DONT_USE:
        cmd += ["-X", p]
    for v in FLAVOURS:
        cmd += ["-L", str(out / f"asap7_{v}_{a.leakage_corner}.lib")]
    stamp.unlink(missing_ok=True)
    tmp = prim.with_suffix(".lib.tmp")
    cmd[3] = str(tmp)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    tmp.replace(prim)  # atomic: a run reading the library never sees half
    stamp.write_text(want)
    print(f"[asap7-mvt] {prim.name} (W {a.leak_weight} um^2/pW, "
          f"{a.leakage_corner} leakage)")


if __name__ == "__main__":
    main()

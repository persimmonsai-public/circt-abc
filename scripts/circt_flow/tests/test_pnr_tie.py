"""`pnr.tie_hi` / `pnr.tie_lo` override the pack's tie cells (stages.orfs_pnr)."""
from types import SimpleNamespace

from circt_flow import orfs_pnr as pnr_mod
from circt_flow import stages


def _run(tmp_path, monkeypatch, pnr, tie):
    net = tmp_path / "n.v"
    net.write_text("")
    cfg = SimpleNamespace(
        design=SimpleNamespace(pnr=pnr, tie=tie, top_module="t",
                               clock_period_ps=1000, pack_pnr={}),
        openroad_netlist=net, pnr_dir=tmp_path / "pnr")
    seen = {}
    monkeypatch.setattr(pnr_mod, "main",
                        lambda argv: seen.setdefault("argv", argv) and 0)
    stages.orfs_pnr(cfg)
    return seen["argv"]


def _tie(argv):
    return (argv[argv.index("--tie-hi") + 1] if "--tie-hi" in argv else None,
            argv[argv.index("--tie-lo") + 1] if "--tie-lo" in argv else None)


def test_pack_tie_cells_by_default(tmp_path, monkeypatch):
    argv = _run(tmp_path, monkeypatch, {"clock_port": "clk"},
                {"hi": "TH:H", "lo": "TL:L"})
    assert _tie(argv) == ("TH:H", "TL:L")


def test_pnr_tie_overrides_pack(tmp_path, monkeypatch):
    argv = _run(tmp_path, monkeypatch,
                {"clock_port": "clk", "tie_hi": "XH:H", "tie_lo": "XL:L"},
                {"hi": "TH:H", "lo": "TL:L"})
    assert _tie(argv) == ("XH:H", "XL:L")


def test_pnr_tie_without_pack_tie(tmp_path, monkeypatch):
    argv = _run(tmp_path, monkeypatch,
                {"clock_port": "clk", "tie_hi": "XH:H", "tie_lo": "XL:L"}, None)
    assert _tie(argv) == ("XH:H", "XL:L")


def test_no_tie_cells_no_flags(tmp_path, monkeypatch):
    argv = _run(tmp_path, monkeypatch, {"clock_port": "clk"}, None)
    assert _tie(argv) == (None, None)

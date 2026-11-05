"""frontend.fetch_source under parallel callers: flows started together on a
cold cache (examples/asap7-mvt/run.sh starts four) fetch the design's RTL
once, and none of them sees a half-made checkout."""
import subprocess
import sys
import textwrap
from pathlib import Path

from circt_flow.config import ROOT

CHILD = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from circt_flow import frontend
    log = Path(sys.argv[1]); design = {"name": "d", "source": {
        "git": sys.argv[2], "commit": sys.argv[3]}}
    run = frontend._run
    def slow(cmd, **kw):
        if "fetch" in cmd:                     # widen the window
            with log.open("a") as f: f.write("fetch\\n")
            time.sleep(1.0)
        run(cmd, **kw)
    frontend._run = slow
    print(frontend.fetch_source(design, None))
""")


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_parallel_fetch_runs_once(tmp_path):
    repo = tmp_path / "rtl"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.sv").write_text("module a; endmodule\n")
    _git("add", ".", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "-m", "rtl", cwd=repo)
    commit = _git("rev-parse", "HEAD", cwd=repo)
    # a local clone fetched by hash needs this
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=repo)
    log = tmp_path / "fetches"
    env = {"PATH": __import__("os").environ["PATH"],
           "CIRCT_ABC_CACHE": str(tmp_path / "cache"),
           "PYTHONPATH": str(ROOT / "scripts")}
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(log),
                               str(repo), commit], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
             for _ in range(4)]
    res = [(p.wait(), *p.communicate()) for p in procs]
    for rc, out, err in res:
        assert rc == 0, err
    paths = {out.strip().splitlines()[-1] for _, out, _ in res}
    assert len(paths) == 1
    assert (Path(paths.pop()) / "a.sv").is_file()
    assert log.read_text().count("fetch") == 1

# -*- Python -*-
# lit configuration for the circt-abc mapper tests.
#
# Deliberately smaller than CIRCT's own lit.cfg.py: every RUN line in this
# suite invokes exactly two tools, `circt-abc-opt` piped into `FileCheck`,
# so there is nothing else to substitute.

import os
import lit.formats
from lit.llvm import llvm_config

config.name = "circt-abc"
config.test_format = lit.formats.ShTest()

# Only .mlir. The .lib files beside some tests are hermetic liberty fixtures
# passed to a pass option, not tests in their own right -- listing `.lib`
# here (as CIRCT does) would make lit try to execute them.
config.suffixes = [".mlir"]

config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.circt_abc_obj_root, "test")

config.substitutions.append(("%PATH%", config.environment["PATH"]))

llvm_config.use_default_substitutions()
llvm_config.with_system_environment(["HOME", "INCLUDE", "LIB", "TMP", "TEMP"])

llvm_config.with_environment(
    "PATH", [config.circt_abc_tools_dir, config.llvm_tools_dir], append_path=True
)

tools = ["circt-abc-opt", "FileCheck"]
llvm_config.add_tool_substitutions(
    tools, [config.circt_abc_tools_dir, config.llvm_tools_dir]
)

# Several tests pass a hermetic liberty to a pass option and refer to it
# relative to the test file. %S already covers that, but make the directory
# explicit for readability in new tests.
config.substitutions.append(("%test_dir", config.test_source_root))

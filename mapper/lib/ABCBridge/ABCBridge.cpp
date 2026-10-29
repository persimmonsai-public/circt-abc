//===- ABCBridge.cpp ----------------------------------------*- C++ -*-===//
//
// Automates the recipe validated by hand in RECIPE.md's "Full mechanism
// validated end-to-end" section, so residual (structural-mapper-unmatched)
// combinational logic can be technology-mapped to real liberty cells
// via a real ABC subprocess, with NO Yosys anywhere in the loop:
//
//   1. Clone the target hw.module into a scratch top-level module.
//   2. `convert-comb-to-synth`: lower comb ops to `synth.aig.and_inv` (an
//      ordinary MLIR pass, run via a nested PassManager).
//   3. `circt::aiger::exportAIGER`: write a real AIGER file (the same API
//      CIRCT's own AIGERRunner pass uses).
//   4. Invoke a real ABC binary as a subprocess with the reference Yosys
//      flow's tuned recipe (transcribed from its synth.tcl, see RECIPE.md) --
//      `read_lib`+`read_aiger`+the strash/scorr/dc2/&dch/&nf script +
//      `write_verilog`. ABC's own liberty reader handles real PDK liberty
//      syntax fine (Yosys's/CIRCT's own liberty frontends do not always).
//   5. Re-import ABC's mapped Verilog (which names real liberty cells) plus a
//      blackbox-stub declaration file, in-process via `circt::importVerilog`
//      (mode=Full) -- no `circt-verilog` subprocess needed.
//   6. Externalize every cell-placeholder `hw.module` (everything in the
//      reimport EXCEPT the target module itself) into `hw.module.extern` --
//      circt-verilog synthesizes a definite (constant-0) driver for an
//      empty-body module's unassigned outputs, same root cause as
//      ExternalizeMappedCellsPass and scripts/externalize_stub_cells.py
//      both already had to work around elsewhere in this project.
//   7. Splice the reimported target module's body into the ORIGINAL
//      target module (same symbol name, so existing hw.instance references
//      elsewhere keep working), and clone the newly-created extern
//      declarations into the real top-level module (deduplicated by name
//      across multiple target modules / repeated runs).
//
//===----------------------------------------------------------------------===//

#include <algorithm>
#include "circt-abc/Passes.h"

#include "circt/Conversion/CombToSynth.h"
#include "circt/Conversion/CombToDatapath.h"
#include "circt/Conversion/DatapathToComb.h"
#include "circt/Dialect/Datapath/DatapathPasses.h"
#include "circt/Conversion/ExportAIGER.h"
#include "circt/Dialect/Synth/Transforms/SynthPasses.h"
#include "circt/Conversion/ImportVerilog.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/HW/HWPasses.h"
#include "circt/Dialect/Synth/SynthDialect.h"
#include "circt/Dialect/Datapath/DatapathDialect.h"
#include "circt/Dialect/Debug/DebugDialect.h"
#include "circt/Dialect/LLHD/LLHDDialect.h"
#include "circt/Dialect/Moore/MooreDialect.h"
#include "circt/Dialect/Seq/SeqDialect.h"
#include "circt/Dialect/Sim/SimDialect.h"
#include "circt/Dialect/Verif/VerifDialect.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/ControlFlow/IR/ControlFlow.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/LLVMIR/LLVMDialect.h"
#include "mlir/Dialect/Math/IR/Math.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/Dialect/UB/IR/UBOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Diagnostics.h"
#include "mlir/IR/IRMapping.h"
#include "mlir/IR/ImplicitLocOpBuilder.h"
#include "mlir/IR/OwningOpRef.h"
#include "mlir/Pass/PassManager.h"
#include "mlir/Pass/PassRegistry.h"
#include "mlir/Support/FileUtilities.h"
#include "mlir/Support/Timing.h"
#include "mlir/Transforms/Passes.h"
#include "llvm/ADT/ScopeExit.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/Support/DynamicLibrary.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Program.h"
#include "llvm/Support/Process.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/Support/ToolOutputFile.h"
#include "llvm/Support/raw_ostream.h"
#include <cctype>
#include <cstdio>
#include <cstring>
#ifndef _WIN32
#include <fcntl.h>
#include <unistd.h>
#endif

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {
// AIGER carries bit-level combinational logic only. Two things genuinely
// break its exporter, both observed on OpenC910:
//   * an op outside the `hw`/`comb` dialects -- a leftover `llhd.sig`/`drv`
//     that sig2reg declined to promote, or a `seq.to_clock`
//     ("unhandled operation", then "AIGER export failed").
//   * `hw.aggregate_constant`, which is a `hw` op and so slips past the
//     dialect check, but has no bit-level form.
//
// An aggregate-TYPED value does NOT belong on that list, even though an
// earlier revision of this predicate rejected one. Checked directly:
// `ct_prio_12_residual` carries `hw.bitcast (i4) -> !hw.array<2xi2>` feeding a
// `comb.mux` on the array type, and abc-bridge maps it end to end without
// complaint -- CombToSynth reduces the bitcast away. Rejecting on type alone
// blackboxed 21 instantiations of the ct_prio family that were always
// mappable.
static bool isUnmappableToAiger(hw::HWModuleOp m) {
  bool bad = false;
  m.getBodyBlock()->walk([&](Operation *op) {
    StringRef dialect = op->getName().getDialectNamespace();
    // hw.aggregate_constant used to be rejected here ("has no bit-level
    // form"), and that was true when written -- but CombToSynth has since
    // gained the aggregate lowerings (it marks AggregateConstantOp illegal
    // and converts it), and a direct test maps CVA6's branch_unit_residual
    // (a struct-typed output fed by an aggregate zero) with exit 0. The
    // rejection had re-blackboxed 15 CVA6 residuals whose only offense was
    // a struct-typed module output. Same lesson as M9, second occurrence:
    // an unmeasured conservative guard silently deletes logic.
    if (dialect != "hw" && dialect != "comb") {
      bad = true;
      return WalkResult::interrupt();
    }
    return WalkResult::advance();
  });
  return bad;
}


// Scans ABC's mapped Verilog for instantiated cell type names (Verilog
// instantiation syntax: `<type> <instance-name> ( ... );`, one per line in
// ABC's write_verilog output) so we only need to pull IN the handful of
// stub declarations actually used, rather than every cell in the PDK --
// simpler and faster, and avoids feeding circt-verilog ~1000 unrelated
// top-level modules alongside the one we actually want (confirmed this
// session: doing so left `--top <target>` unable to find the target
// module at all, for reasons not fully root-caused -- filtering sidesteps
// the question rather than chasing a possible CIRCT top-module-selection
// limitation).
llvm::StringSet<> extractInstantiatedCellNames(StringRef verilogText) {
  llvm::StringSet<> names;
  for (StringRef line : llvm::split(verilogText, '\n')) {
    line = line.trim();
    if (line.empty() || line.starts_with("//") || line.starts_with("module") ||
        line.starts_with("input") || line.starts_with("output") ||
        line.starts_with("endmodule") || line.starts_with("wire") ||
        line.starts_with("assign"))
      continue;
    size_t typeEnd = 0;
    while (typeEnd < line.size() &&
           (isalnum((unsigned char)line[typeEnd]) || line[typeEnd] == '_'))
      ++typeEnd;
    if (typeEnd == 0)
      continue;
    StringRef rest = line.drop_front(typeEnd);
    if (rest.empty() || !isspace((unsigned char)rest.front()))
      continue; // not `<ident><space>...` -- e.g. a bare `input a, b;` line
    if (!rest.trim().contains('('))
      continue;
    names.insert(line.take_front(typeEnd));
  }
  return names;
}

// Pulls just the `module NAME ( ... ) ... endmodule` blocks named in `wanted`
// out of `stubsText` (gen_liberty_blackbox_stubs.py's output: a flat sequence
// of such blocks, one per PDK cell, never nested).
std::string extractStubDeclarations(StringRef stubsText,
                                     const llvm::StringSet<> &wanted) {
  std::string result;
  size_t pos = 0;
  while (true) {
    size_t modKw = stubsText.find("module ", pos);
    if (modKw == StringRef::npos)
      break;
    size_t nameStart = modKw + strlen("module ");
    size_t nameEnd = nameStart;
    while (nameEnd < stubsText.size() &&
           (isalnum((unsigned char)stubsText[nameEnd]) || stubsText[nameEnd] == '_'))
      ++nameEnd;
    StringRef name = stubsText.slice(nameStart, nameEnd);
    size_t endMod = stubsText.find("endmodule", nameEnd);
    if (endMod == StringRef::npos) {
      pos = nameEnd;
      continue;
    }
    size_t blockEnd = endMod + strlen("endmodule");
    if (wanted.contains(name))
      result += stubsText.slice(modKw, blockEnd).str() + "\n";
    pos = blockEnd;
  }
  return result;
}

// `comb.icmp`'s `weq`/`wne` ("wildcard equal/not-equal", from casez-style
// SV constructs) predicates are rejected by convert-comb-to-synth
// (confirmed on the real ibex design: `ibex_controller`'s
// `instr_gets_expanded_i` case statement). They are always SAFE to
// rewrite to plain `ceq`/`cne` here: HW/Comb is a strictly 2-state
// dialect (no X/Z bit representation exists at this level at all --
// unlike Moore dialect upstream, which does), so a `weq`/`wne` surviving
// down to Comb is semantically identical to `ceq`/`cne` unconditionally,
// not just when the compared constant happens to have no wildcard bits
// (there's no OTHER way to interpret a wildcard-equality against a
// concrete 2-state value) -- confirmed directly in CombFolds.cpp, whose
// own constant-folding evaluator maps `weq`/`wne` to exactly the same
// `lhs.eq(rhs)`/`lhs.ne(rhs)` as `ceq`/`cne`.
void legalizeWildcardIcmp(hw::HWModuleOp module) {
  module.walk([](comb::ICmpOp icmp) {
    if (icmp.getPredicate() == comb::ICmpPredicate::weq)
      icmp.setPredicate(comb::ICmpPredicate::ceq);
    else if (icmp.getPredicate() == comb::ICmpPredicate::wne)
      icmp.setPredicate(comb::ICmpPredicate::cne);
  });
}

// `hw-aggregate-to-comb` doesn't eliminate `hw.struct_inject` when its
// base struct comes from a `hw.bitcast` rather than a plain
// `hw.struct_create` (confirmed on the real ibex design:
// `ibex_cs_registers`' mstatus CSR field-update logic reads
// `bitcast(flat) -> struct`, injects one field, and presumably bitcasts
// back -- looping hw-aggregate-to-comb+canonicalize+CSE several times
// did not resolve it either). Rewrite it into the semantically
// equivalent "extract every OTHER field, then struct_create with the
// injected one substituted in" form instead -- ops hw-aggregate-to-comb
// DOES already handle (struct_extract/struct_create), so this sidesteps
// needing to hand-compute struct field bit-layout/packing ourselves.
void legalizeStructInject(hw::HWModuleOp module) {
  for (auto injectOp : llvm::make_early_inc_range(
           module.getBodyBlock()->getOps<hw::StructInjectOp>())) {
    auto structTy = cast<hw::StructType>(injectOp.getType());
    ArrayRef<hw::StructType::FieldInfo> fields = structTy.getElements();
    ImplicitLocOpBuilder b(injectOp.getLoc(), injectOp);
    SmallVector<Value> fieldValues;
    for (auto [idx, field] : llvm::enumerate(fields)) {
      if (static_cast<int64_t>(idx) == injectOp.getFieldIndex())
        fieldValues.push_back(injectOp.getNewValue());
      else
        fieldValues.push_back(
            hw::StructExtractOp::create(b, injectOp.getInput(), field.name));
    }
    Value newStruct = hw::StructCreateOp::create(b, structTy, fieldValues);
    injectOp.getResult().replaceAllUsesWith(newStruct);
    injectOp.erase();
  }
}

// `convert-comb-to-synth` (this file's step 2) cannot lower
// `hw.aggregate_constant` (confirmed on the real ibex design: `ibex_alu`'s
// outlined residual module has one feeding an all-zero `!hw.array<2xi32>`
// port default) -- `--hw-aggregate-to-comb` upstream in
// scripts/run_circt_native_synth.sh's pipeline already converts most
// array/struct manipulation into plain comb ops, but a bare constant with
// no consuming array_get/struct_extract to fuse with has nothing for that
// pass's rewrite patterns to match, so it survives untouched. Replace any
// that remain with `hw.bitcast(hw.constant)`: bitcasting the aggregate
// constant to a same-size flat integer type folds directly to a genuine
// `hw.constant` (`hw::BitcastOp` has `hasFolder = true`, the exact
// mechanism RegisterMapperPass's resetValue handling already relies on),
// then bitcasting that back to the original type gives an equivalent
// value convert-comb-to-synth's AIG lowering can actually handle (a plain
// scalar constant under a bitcast, not an aggregate op).
void legalizeAggregateConstants(hw::HWModuleOp module) {
  for (auto constOp :
       llvm::make_early_inc_range(module.getBodyBlock()->getOps<hw::AggregateConstantOp>())) {
    Type origType = constOp.getType();
    int64_t bitWidth = hw::getBitWidth(origType);
    if (bitWidth <= 0)
      continue; // Shouldn't happen for a real constant; leave as-is if so.
    OpBuilder builder(constOp);
    Type flatType = builder.getIntegerType(bitWidth);
    Value flat = builder.createOrFold<hw::BitcastOp>(constOp.getLoc(), flatType,
                                                       constOp.getResult());
    Value back = hw::BitcastOp::create(builder, constOp.getLoc(), origType, flat);
    constOp.getResult().replaceAllUsesWith(back);
    constOp.erase();
  }
}

// AIGER export (and convert-comb-to-synth before it) cannot handle ANY
// non-integer type or an `hw.bitcast` op, full stop -- confirmed on the
// real ibex design (`ibex_alu`'s `imd_val_q_i : !hw.array<2xi32>` port).
// `--hw-flatten-io` only covers struct-typed ports (not arrays, per its
// own --help text) and fails across `hw.instance` boundaries design-wide
// anyway (confirmed separately) -- but `module` here is guaranteed
// instance-free (it's either an outline-residual-logic `_residual`
// submodule, or a genuinely-leaf all-comb module) and about to be
// discarded once AIG export prep is done, so rebuilding it from scratch
// with an all-integer port signature is safe: change every non-integer
// port's type to a same-bit-width integer, and bridge the difference
// with `hw.bitcast` immediately inside the new body (input side) and
// immediately before the terminator (output side). A subsequent
// `--hw-aggregate-to-comb` run (this file's caller already does one, see
// runOnModule) then has a chance to fold any `array_get`/`struct_extract`
// consuming these boundary bitcasts down to plain `comb.extract` on the
// now-flat argument directly, leaving no aggregate types or bitcasts
// anywhere in the module by the time convert-comb-to-synth runs.
hw::HWModuleOp flattenPortsToFlatIntegers(hw::HWModuleOp module) {
  MLIRContext *ctx = module.getContext();
  SmallVector<hw::PortInfo> newPorts = module.getPortList();
  bool anyChanged = false;
  for (hw::PortInfo &port : newPorts) {
    if (!isa<IntegerType>(port.type)) {
      int64_t bitWidth = hw::getBitWidth(port.type);
      if (bitWidth > 0) {
        port.type = IntegerType::get(ctx, bitWidth);
        anyChanged = true;
      }
    }
  }
  if (!anyChanged)
    return module;

  Location loc = module.getLoc();
  OpBuilder builder(module);
  auto newModule =
      hw::HWModuleOp::create(builder, loc, module.getModuleNameAttr(), newPorts);
  newModule.setPrivate();

  Block *oldBody = module.getBodyBlock();
  Block *newBody = newModule.getBodyBlock();
  OpBuilder bodyBuilder = OpBuilder::atBlockBegin(newBody);
  IRMapping mapping;
  for (auto [oldArg, newArg] :
       llvm::zip(oldBody->getArguments(), newBody->getArguments())) {
    Value mapped = newArg;
    if (oldArg.getType() != newArg.getType())
      mapped = hw::BitcastOp::create(bodyBuilder, oldArg.getLoc(),
                                      oldArg.getType(), newArg);
    mapping.map(oldArg, mapped);
  }

  // Clone in two phases, for the same reason OutlineResidualLogicPass does:
  // the body is not guaranteed to be in def-before-use order, and once
  // outlining runs on a FLATTENED module the graph is cyclic at word level
  // (false cycles through comb.concat/extract over disjoint bit ranges), so
  // no ordering exists. A single-phase clone silently leaves such an operand
  // pointing at the OLD body's value; the new module then holds a use of an
  // old op, and the `module.erase()` below aborts the process with
  // "operation destroyed but still has uses". Phase 1 populates the mapping
  // for every value, phase 2 resolves the stragglers.
  SmallVector<Operation *> oldOps;
  for (Operation &op : *oldBody)
    if (!isa<hw::OutputOp>(op))
      oldOps.push_back(&op);

  for (Operation *op : oldOps)
    bodyBuilder.clone(*op, mapping);

  for (Operation *op : oldOps)
    if (Operation *cloned = mapping.lookupOrNull(op))
      // Nested regions too -- see OutlineResidualLogicPass.
      cloned->walk([&](Operation *nested) {
        for (OpOperand &operand : nested->getOpOperands())
          if (Value mapped = mapping.lookupOrNull(operand.get()))
            operand.set(mapped);
      });

  auto oldTerm = cast<hw::OutputOp>(oldBody->getTerminator());
  SmallVector<Type> outputTypes;
  for (hw::PortInfo &p : newPorts)
    if (p.dir == hw::ModulePort::Direction::Output)
      outputTypes.push_back(p.type);

  SmallVector<Value> newOutputs;
  for (auto [oldOutVal, newType] :
       llvm::zip(oldTerm.getOutputs(), outputTypes)) {
    Value mapped = mapping.lookup(oldOutVal);
    if (mapped.getType() != newType)
      mapped =
          hw::BitcastOp::create(bodyBuilder, oldOutVal.getLoc(), newType, mapped);
    newOutputs.push_back(mapped);
  }
  newBody->getTerminator()->setOperands(newOutputs);

  // Break every reference in the old body before erasing it. Its ops can be
  // mutually referencing: once outline-residual-logic runs on a FLATTENED
  // module, the residual graph is cyclic at word level (false cycles through
  // comb.concat/extract over disjoint bit ranges -- see that pass's clone
  // comment). Erasing a region whose ops still use each other aborts the
  // process with "operation destroyed but still has uses"; dropping the
  // references first makes the erase order-independent.
  module.getBodyBlock()->walk([](Operation *op) { op->dropAllReferences(); });
  module.erase();
  return newModule;
}

// Every module inside `import` except `keepName` is a cell placeholder
// (whatever ABC's write_verilog named, resolved against the stub
// declarations) -- convert each into a true hw.module.extern. Same rewrite
// as ExternalizeMappedCellsPass, inlined here so ABCBridge doesn't need a
// second pass invocation on a throwaway scratch module.
void externalizeAllExcept(ModuleOp import, StringRef keepName) {
  for (auto hwModule :
       llvm::make_early_inc_range(import.getOps<hw::HWModuleOp>())) {
    if (hwModule.getModuleName() == keepName)
      continue;
    OpBuilder builder(hwModule);
    hw::HWModuleExternOp::create(builder, hwModule->getLoc(),
                                  hwModule.getModuleNameAttr(),
                                  hwModule.getPortList(), StringRef(),
                                  hwModule.getParameters());
    hwModule->erase();
  }
}

// RAII redirect of the process's own stdout (fd 1) into a file, restored
// on scope exit. Needed only for the in-process ABC path: ABC prints
// through raw `printf` (`Abc_Print`, abc_global.h), NOT through its own
// frame's output stream, so in-process it writes straight into
// circt-abc-opt's stdout -- which is where the tool may be emitting MLIR.
// The subprocess path has the same raw-printf behaviour but in a separate
// process whose stdout the parent never writes IR to at the same time.
// Capturing it keeps the log (echoed to stderr by the caller) without any
// risk of interleaving it into an IR stream. Best-effort: if the dup/open
// fails we simply leave stdout alone rather than failing the pass.
struct StdoutRedirect {
  int savedFd = -1;
  explicit StdoutRedirect(StringRef path) {
#ifndef _WIN32
    fflush(stdout);
    savedFd = dup(STDOUT_FILENO);
    if (savedFd < 0)
      return;
    int fd = open(path.str().c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
      close(savedFd);
      savedFd = -1;
      return;
    }
    dup2(fd, STDOUT_FILENO);
    close(fd);
#endif
  }
  ~StdoutRedirect() {
#ifndef _WIN32
    fflush(stdout);
    if (savedFd >= 0) {
      dup2(savedFd, STDOUT_FILENO);
      close(savedFd);
    }
#endif
  }
  StdoutRedirect(const StdoutRedirect &) = delete;
  StdoutRedirect &operator=(const StdoutRedirect &) = delete;
};

// Runs `script` through ABC IN-PROCESS, via the extern "C" shim built by
// scripts/circt_flow/abc_shim/build.sh (libabc_shim.so -- ABC's whole
// static archive plus a three-function wrapper over
// Abc_Start/Cmd_CommandExecute/Abc_Stop). Loaded with dlopen rather than
// link-time: the ABC archive is a *Yosys* build product (49 MB, 24 static
// ctors), so link-time coupling would make circt-abc-opt unbuildable
// without a prior Yosys build even for users who never enable this
// option. dlopen keeps the cost strictly opt-in -- nothing is loaded when
// --abc-bridge's in-process-abc option is off (the default).
//
// The script here is byte-identical to the one the subprocess path
// passes to `abc -q`, including its own `read_lib`/`read_aiger`/
// `write_verilog` file steps: only the invocation mechanism differs, so
// the two paths are directly diffable on their mapped.v output.
//
// Caveats, both verified in the pre-implementation risk study:
//  - ABC is built without -DNDEBUG (22k+ assert() sites) and a handful of
//    its paths call exit(); an ABC internal failure therefore aborts
//    circt-abc-opt instead of returning. None of the reachable exit()
//    sites are on this recipe's commands, and the same failure would kill
//    the subprocess today -- it is just uncontained here. This is the
//    main reason the option is off by default.
//  - Cmd_CommandExecute's status is real (it stops at the first failing
//    command), unlike `Abc_RealMain`, which returns 0 in batch mode
//    regardless -- so, unlike the subprocess path's, the rc check below
//    actually catches ABC command failures.
LogicalResult runAbcInProcess(Location loc, StringRef shimPath,
                              StringRef script, StringRef logPath) {
  if (shimPath.empty())
    return emitError(loc,
                     "in-process ABC requested but no shim library given -- "
                     "pass abc-shim-path=<...>/libabc_shim.so (build it with "
                     "scripts/circt_flow/abc_shim/build.sh)");

  std::string loadError;
  llvm::sys::DynamicLibrary lib = llvm::sys::DynamicLibrary::getPermanentLibrary(
      shimPath.str().c_str(), &loadError);
  if (!lib.isValid())
    return emitError(loc, "failed to load ABC shim '")
           << shimPath << "': " << loadError;

  auto start =
      reinterpret_cast<void (*)()>(lib.getAddressOfSymbol("abc_shim_start"));
  auto execute = reinterpret_cast<int (*)(const char *)>(
      lib.getAddressOfSymbol("abc_shim_execute"));
  auto stop =
      reinterpret_cast<void (*)()>(lib.getAddressOfSymbol("abc_shim_stop"));
  if (!start || !execute || !stop)
    return emitError(loc, "ABC shim '")
           << shimPath
           << "' is missing one of abc_shim_start/abc_shim_execute/"
              "abc_shim_stop";

  // Start/Stop around every invocation, not once per process:
  // Abc_FrameDeallocate clears the global frame pointer, so repeated
  // cycles are clean, and this keeps N>1 target modules per run working
  // exactly like N separate subprocesses would.
  int rc = 0;
  {
    StdoutRedirect redirect(logPath);
    start();
    rc = execute(script.str().c_str());
    fflush(stdout);
    stop();
  }

  // Echo whatever ABC printed, so its (routine, documented) warnings stay
  // visible exactly as they are with the subprocess -- on stderr, where
  // MLIR's own diagnostics already go.
  if (auto logBuf = llvm::MemoryBuffer::getFile(logPath))
    if (!(*logBuf)->getBuffer().empty())
      llvm::errs() << (*logBuf)->getBuffer();

  if (rc != 0)
    return emitError(loc, "in-process ABC failed (rc=")
           << rc << ")\nscript: " << script;
  return success();
}

struct ABCBridgePass : public PassWrapper<ABCBridgePass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ABCBridgePass)

  ABCBridgePass() = default;
  ABCBridgePass(const ABCBridgePass &other) : PassWrapper(other) {
    libertyPath = other.libertyPath.getValue();
    abcPath = other.abcPath.getValue();
    stubsPath = other.stubsPath.getValue();
    clockPeriodPs = other.clockPeriodPs.getValue();
    targetModule = other.targetModule.getValue();
    enableClassicalResub = other.enableClassicalResub.getValue();
    // MLIR clones passes through this copy ctor. An Option omitted here
    // silently reverts to its init() default with NO diagnostic -- so a
    // caller passing in-process-abc=true would get the subprocess path
    // and never know. Keep every Option above in sync with the
    // declarations below.
    enableInProcessAbc = other.enableInProcessAbc.getValue();
    skipUnmappable = other.skipUnmappable.getValue();
    emitTargetOnly = other.emitTargetOnly.getValue();
    abcShimPath = other.abcShimPath.getValue();
    abcScriptFile = other.abcScriptFile.getValue();
    readLibArgs = other.readLibArgs.getValue();
    useDatapath = other.useDatapath.getValue();
    datapathReduceDelay = other.datapathReduceDelay.getValue();
    datapathTimingAware = other.datapathTimingAware.getValue();
    datapathForceBooth = other.datapathForceBooth.getValue();
    saveMappedDir = other.saveMappedDir.getValue();
    saveAigDir = other.saveAigDir.getValue();
    useMappedDir = other.useMappedDir.getValue();
    useMappedIfPresent = other.useMappedIfPresent.getValue();
  }

  StringRef getArgument() const final { return "abc-bridge"; }
  StringRef getDescription() const final {
    return "Map residual comb logic to real liberty cells via a real "
           "ABC subprocess (no Yosys)";
  }

  // Not auto-populated from Passes.td's `dependentDialects` field (this
  // pass, like its siblings in mapper/lib/StructuralMapper/, is a
  // hand-rolled PassWrapper rather than a TableGen-generated `*Base<>`
  // subclass, so that field is only informative documentation here, not
  // load-bearing). Must be declared manually: the nested PassManager this
  // pass runs internally (convert-comb-to-synth) needs the Synth dialect
  // loaded, and MLIR hard-aborts if a dialect gets lazily loaded once
  // pass-pipeline execution has begun without having been declared here up
  // front.
  void getDependentDialects(DialectRegistry &registry) const override {
    // Synth: needed by our own nested convert-comb-to-synth invocation.
    // The rest: everything circt-verilog's own tool registers (see
    // tools/circt-verilog/circt-verilog.cpp) -- our in-process
    // importVerilog() call needs the same set available since it goes
    // through the identical Verilog->Moore->core-dialects pipeline.
    registry.insert<circt::datapath::DatapathDialect>();
    registry.insert<synth::SynthDialect, mlir::arith::ArithDialect,
                     mlir::cf::ControlFlowDialect, comb::CombDialect,
                     debug::DebugDialect, mlir::func::FuncDialect,
                     hw::HWDialect, llhd::LLHDDialect, mlir::LLVM::LLVMDialect,
                     mlir::math::MathDialect, moore::MooreDialect,
                     mlir::scf::SCFDialect, seq::SeqDialect, sim::SimDialect,
                     mlir::ub::UBDialect, verif::VerifDialect>();
  }

  Option<std::string> libertyPath{*this, "liberty", llvm::cl::desc("")};
  Option<std::string> abcPath{*this, "abc-path",
                               llvm::cl::init("abc"), llvm::cl::desc("")};
  Option<std::string> stubsPath{*this, "stubs", llvm::cl::desc("")};
  Option<unsigned> clockPeriodPs{*this, "clock-period-ps",
                                  llvm::cl::init(1000), llvm::cl::desc("")};
  Option<std::string> targetModule{*this, "target", llvm::cl::desc("")};
  // Skip, rather than fail on, a residual module carrying ops AIGER cannot
  // represent -- an `llhd.sig`/`drv`/`prb` that sig2reg declined to promote,
  // or an aggregate constant feeding one. Those modules stay behavioural
  // instead of being gate-mapped, so the netlist is INCOMPLETE and this is
  // off by default; it exists so a large design can be taken as far as it
  // goes and the residue measured, rather than the whole run aborting on the
  // first unmappable module. Every skip is reported.
  // Emit ONLY the mapped target (plus its `_bits` companion and the extern
  // cell declarations), discarding the rest of the design. For partitioned
  // runs: each worker maps one group but would otherwise write out the whole
  // design, and the splicer only ever reads back its one module. Measured on
  // OpenC910 at 64 groups: 461 MB written per worker against 56 MB actually
  // needed -- 22 GB of scratch instead of ~3.5 GB. Requires `target`.
  Option<bool> emitTargetOnly{
      *this, "emit-target-only", llvm::cl::init(false),
      llvm::cl::desc("Write only the mapped target module, its `_bits` "
                     "companion and the extern cell declarations. Requires "
                     "target=; intended for partitioned runs whose output is "
                     "spliced back together.")};

  Option<bool> skipUnmappable{
      *this, "skip-unmappable", llvm::cl::init(false),
      llvm::cl::desc("Skip residual modules containing ops AIGER cannot "
                     "export instead of failing. Produces an INCOMPLETE "
                     "netlist; for scale measurement, not for production.")};
  // Tier-2 roadmap item 10: classical (non-`&`) `resub` was found (M1b) to give
  // a small area win on a test run at a cost of +1 logic level after
  // re-mapping, deprioritized at the time because no P&R pipeline existed to
  // check whether that extra level is a real timing problem. Re-evaluated once
  // a real P&R pipeline existed (M3): correctness proven via full-design ABC
  // `cec` (not just a cone -- the whole 6226-input/6615-output AIG, both
  // directions), and real post-P&R numbers show a genuine simultaneous
  // area+timing win, not a tradeoff (smaller area and better WNS). The "+1
  // logic level" risk did not materialize (classical `resub`'s own `-l` flag
  // defaults to preserving level count). Smaller than the original estimate
  // (most of that headroom was already captured by the M1b high-effort recipe
  // fix), but real and risk-verified -- promoted to ON by default.
  Option<bool> enableClassicalResub{
      *this, "enable-classical-resub", llvm::cl::init(true),
      llvm::cl::desc(
          "Also run classical (non-&) `resub` in the ABC recipe, in "
          "old-AIG space right after the existing &resub/&put step "
          "(default on -- see this option's comment for the M3 "
          "re-evaluation that promoted it)")};

  // OFF BY DEFAULT, deliberately: with it off, ABC runs exactly as it
  // always has (a `yosys-abc -q "<script>"` subprocess). With it on, the
  // identical script runs in-process through libabc_shim.so -- same
  // AIGER file in, same mapped.v out, only the invocation mechanism
  // changes. See runAbcInProcess() for the mechanism and its two risks
  // (ABC's assert()/exit() are uncontained in-process).
  Option<bool> enableInProcessAbc{
      *this, "in-process-abc", llvm::cl::init(false),
      llvm::cl::desc(
          "Run ABC in-process via the dlopen'd shim in abc-shim-path "
          "instead of spawning the abc-path binary (default off; the "
          "subprocess path is unchanged and remains the default)")};
  Option<std::string> abcShimPath{
      *this, "abc-shim-path", llvm::cl::init(""),
      llvm::cl::desc("Path to libabc_shim.so (built by "
                     "scripts/circt_flow/abc_shim/build.sh). Required when "
                     "in-process-abc is on; ignored otherwise")};
  // Recipe experimentation without a mapper rebuild. Read from a FILE, not an
  // option value, on purpose: MLIR's pass-option parser tokenizes on
  // whitespace, and an ABC recipe is full of spaces (see the flow skill's
  // "MLIR's own pass-options parser" gotcha). Empty (the default) => the
  // hardcoded recipe below, byte-for-byte. Honoured by BOTH invocation paths,
  // so a custom recipe can be diffed subprocess-vs-in-process -- which is what
  // the byte-identity check needs, since the production recipe's
  // `&deepsyn -T 10` is a CPU-time budget and so is not reproducible across two
  // runs by construction.
  // `&deepsyn`'s CPU-time budget, in seconds. The historical value is 10, which
  // is the default here so the assembled script stays byte-identical unless
  // this is set explicitly. Measured on ibex: raising it to 1200 on the frozen
  // post-recipe AIG takes 16,639 -> 16,104 AND nodes (-3.2%, CEC-equivalent)
  // and shrinks the mapped netlist for a few ps of mapped delay -- a bigger win
  // than every mapper-side lever in this project combined. It converges early
  // (best iteration at 374 s of a 1,200 s budget), so most of the benefit is
  // available well below the full budget. NOTE this makes the run even less
  // reproducible than the default already is: -T is CPU time, so a larger
  // budget means a larger load-dependent spread between instances.
  Option<unsigned> deepsynTimeSec{
      *this, "deepsyn-time-sec", llvm::cl::init(10),
      llvm::cl::desc("CPU-second budget for &deepsyn in the built-in recipe "
                     "(default 10 = the historical script)")};
  Option<std::string> abcScriptFile{
      *this, "abc-script-file", llvm::cl::init(""),
      llvm::cl::desc(
          "File containing the ABC optimization steps to run between "
          "read_lib/read_aiger and write_verilog (';'- or newline-"
          "separated; '#' starts a comment; '{period}' expands to "
          "clock-period-ps). Empty (default) = the built-in recipe")};
  // PHYS-ABC loop support. save-mapped-dir keeps each target's mapped
  // Verilog (module already renamed to the target symbol) at a STABLE
  // path, <dir>/<target>.v, so the placement-aware re-optimization
  // harness can extract per-gate output-net names from it and hand it
  // back to a standalone ABC sizing pass. use-mapped-dir is the return
  // trip: skip running ABC entirely and re-import <dir>/<target>.v as
  // the mapped netlist (bridge steps 5-7 -- stub filtering,
  // importVerilog, externalize, splice -- run unchanged on it).
  // Extra arguments for ABC's `read_lib` (e.g. "-G 10" for logical-effort
  // delays instead of the unit-delay genlib; PHYS-ABC P5). Prepended to
  // the liberty path in the script header, so the body is unaffected.
  Option<bool> useDatapath{
      *this, "use-datapath",
      llvm::cl::desc("lower multi-operand add/sub/mul through the Datapath "
                     "dialect (carry-save compressors) before the AIG conversion"),
      llvm::cl::init(false)};
  // Knobs on the datapath lowering, for premise tests on multiplier area (a
  // mapped 64x64 multiplier core came out larger than a commercial flow's for
  // the same ~2,000-carry tree). Defaults reproduce the shipped pipeline
  // exactly.
  Option<bool> datapathReduceDelay{
      // CLOSED: this and the two options below were measured on both CVA6
      // configurations and every combination lands within about a percent --
      // reduce-delay off, timing-aware off, both off. Booth is already on above
      // the 16-bit threshold, so forcing it changes nothing that matters. Kept
      // configurable because the lowering wants them, but they are not levers.
      *this, "datapath-reduce-delay",
      llvm::cl::desc("run datapath-reduce-delay before lowering (default true)"),
      llvm::cl::init(true)};
  Option<bool> datapathTimingAware{
      *this, "datapath-timing-aware",
      llvm::cl::desc("timing-aware compressor synthesis (default true)"),
      llvm::cl::init(true)};
  Option<bool> datapathForceBooth{
      *this, "datapath-force-booth",
      llvm::cl::desc("force Booth partial products for every multiply"),
      llvm::cl::init(false)};
  Option<std::string> readLibArgs{
      *this, "read-lib-args",
      llvm::cl::desc("extra arguments for ABC read_lib, comma-separated (e.g. \"-G,10\")"),
      llvm::cl::init("")};
  // Per-output required times for `&nf` (the vendored ABC's new `-t`).
  //
  // WHY. `Nf_ManSetOutputRequireds` (giaNf.c:1635) hands EVERY primary
  // output the same required time -- the network's own global critical
  // delay -- unless `fDoAverage` is set:
  //
  //     Required = fDoAverage ? Required * (100 + nRelaxRatio) / 100
  //                           : p->pPars->MapDelay;
  //
  // so a cone whose delay-optimal arrival is 3 levels is told it may take 31,
  // and area recovery then spends that slack, replacing the shallow mapping
  // with the cheapest (and deepest) one that still fits. On ibex that is what
  // produces our depth distribution: a shallow bulk plus a long tail of cones
  // inflated to just under the global critical path. `fDoAverage` gives each
  // output its OWN arrival (times (100+R)/100) instead, which is a proportional
  // slack budget -- exactly the post-mapping depth metric the mapper was
  // missing.
  //
  // Upstream `&nf` has no switch for it (`&if`/`&lf` expose it as `-t`);
  // patches/abc/ (0014) adds one. -1 (the default)
  // leaves the recipe byte-identical to what it has always been.
  Option<int> nfRelaxPerOutput{
      *this, "nf-relax-per-output", llvm::cl::init(-1),
      llvm::cl::desc(
          "if >= 0, map with `&nf -t -R <n>`: each output's required time "
          "is its own delay-optimal arrival plus n%, instead of the "
          "network's global critical delay (-1 = off, upstream behaviour)")};
  // PHYS-ABC P6: keep each target's pre-mapping AIG (the bridge's in.aig)
  // as <dir>/<target>.aig so a group can be re-mapped from its ORIGINAL
  // structure (strashing the mapped netlist is ~3x deeper).
  Option<std::string> saveAigDir{
      *this, "save-aig-dir",
      llvm::cl::desc("save each target's pre-mapping AIGER as <dir>/<target>.aig"),
      llvm::cl::init("")};
  Option<std::string> saveMappedDir{
      *this, "save-mapped-dir", llvm::cl::init(""),
      llvm::cl::desc("Directory to save each mapped target's Verilog as "
                     "<target>.v after ABC (empty = don't save)")};
  Option<bool> useMappedIfPresent{
      *this, "use-mapped-if-present", llvm::cl::init(false),
      llvm::cl::desc("with use-mapped-dir: groups without a saved netlist "
                     "are mapped fresh instead of erroring (partial reuse)")};
  Option<std::string> useMappedDir{
      *this, "use-mapped-dir", llvm::cl::init(""),
      llvm::cl::desc("Skip ABC and re-import <dir>/<target>.v as the mapped "
                     "netlist (the physical re-optimization loop's return "
                     "path; empty = run ABC normally)")};

  // Reads abc-script-file and flattens it into one ';'-separated ABC
  // command string. Comments and blank lines are dropped; '{period}' is
  // substituted. Returns failure (with a diagnostic) if the file cannot
  // be read or contains no commands.
  LogicalResult readScriptFile(Location loc, std::string &out) {
    auto buf = llvm::MemoryBuffer::getFile(abcScriptFile.getValue());
    if (!buf)
      return emitError(loc, "failed to read --abc-script-file: ")
             << abcScriptFile.getValue() << ": " << buf.getError().message();
    SmallVector<StringRef> lines;
    (*buf)->getBuffer().split(lines, '\n');
    std::string joined;
    for (StringRef line : lines) {
      line = line.substr(0, line.find('#')).trim();
      if (line.empty())
        continue;
      if (!joined.empty())
        joined += "; ";
      joined += line.str();
      while (!joined.empty() && (joined.back() == ';' || joined.back() == ' '))
        joined.pop_back();
    }
    if (joined.empty())
      return emitError(loc, "--abc-script-file has no commands: ")
             << abcScriptFile.getValue();
    std::string period = std::to_string(clockPeriodPs.getValue());
    for (size_t pos = joined.find("{period}"); pos != std::string::npos;
         pos = joined.find("{period}", pos + period.size()))
      joined.replace(pos, strlen("{period}"), period);
    out = std::move(joined);
    return success();
  }

  LogicalResult runOnModule(hw::HWModuleOp target, ModuleOp topModule) {
    MLIRContext *ctx = &getContext();
    Location loc = target.getLoc();

    // A module with no OUTPUT ports computes nothing observable: there is
    // nothing to map. It happens when a partition's logic was optimised away
    // entirely -- with one group per RTL module (isolate-each), cv32a60x
    // produces four, once sequential constant propagation deletes the tied-
    // off prefetcher. Handed to ABC as an AIGER with no inputs or outputs it
    // aborts on a name-table assertion (abcCheck.c: Abc_NtkCheckNames), and
    // that failed the whole flow. A group with inputs but no outputs (a sink
    // left behind by the same deletion; cv32a60x's pmp_data_if) gets through
    // ABC but fails the mapped-Verilog reimport. Leave such a module as is.
    if (target.getNumOutputPorts() == 0)
      return success();

    SmallString<128> tempDir;
    if (auto err = llvm::sys::fs::createUniqueDirectory("abc-bridge", tempDir))
      return emitError(loc, "failed to create temp dir: ") << err.message();
    auto removeTempDir = llvm::scope_exit([&] {
      if (!llvm::sys::Process::GetEnv("ABC_BRIDGE_KEEP_TEMP"))
        llvm::sys::fs::remove_directories(tempDir);
      else
        llvm::errs() << "[abc-bridge] kept temp dir: " << tempDir << "\n";
    });

    // --- 1-2. Clone into a scratch module and lower comb -> synth.aig. ---
    // (ModuleOp::create needs a builder with SOME insertion point, even a
    // throwaway one, or the freshly-created op has nowhere to "insert
    // into" -- mirrors CIRCT's own AIGERRunner.cpp import-side pattern.)
    Block anchorBlock1;
    OwningOpRef<ModuleOp> scratch;
    {
      OpBuilder b(ctx);
      b.setInsertionPointToStart(&anchorBlock1);
      scratch = ModuleOp::create(b, loc);
      OpBuilder bodyBuilder(ctx);
      bodyBuilder.setInsertionPointToStart(scratch->getBody());
      auto cloned = cast<hw::HWModuleOp>(bodyBuilder.clone(*target));
      legalizeWildcardIcmp(cloned);
      legalizeStructInject(cloned);
      // Order matters: flatten ports to plain integers FIRST (bridging via
      // hw.bitcast at the boundary), then re-run hw-aggregate-to-comb so it
      // gets a chance to fold any array_get/struct_extract consuming those
      // boundary bitcasts down to plain comb.extract on the now-flat
      // argument directly -- then legalize whatever aggregate constants
      // are still left (ones with no consuming aggregate op for
      // hw-aggregate-to-comb's patterns to match, e.g. a bare all-zero
      // array feeding a port default). See flattenPortsToFlatIntegers's
      // and legalizeAggregateConstants's own comments for why each step
      // is needed -- confirmed necessary in this order on the real ibex
      // design (ibex_alu's imd_val_q_i : !hw.array<2xi32> port).
      cloned = flattenPortsToFlatIntegers(cloned);
      // Unlike the CLI's textual pass-pipeline parser (which auto-nests a
      // bare `--pass-name` under the right anchor op), the raw C++
      // PassManager::addPass() API requires explicit nesting for a pass
      // anchored at something other than the PassManager's own top-level
      // anchor (ModuleOp here) -- HWAggregateToComb is
      // `Pass<"hw-aggregate-to-comb", "hw::HWModuleOp">`, so it must go
      // through `nest<hw::HWModuleOp>()`, confirmed necessary after
      // hitting "trying to schedule pass 'HWAggregateToComb' on an
      // unsupported operation" without it.
      // Run this trio a few times, not once: a chained pattern like
      // `hw.struct_inject(hw.bitcast(flatArg), ...)` (confirmed on the
      // real ibex design, ibex_cs_registers' mstatus field update logic)
      // may need canonicalize to simplify the bitcast first before
      // hw-aggregate-to-comb's own patterns can recognize and eliminate
      // the struct_inject on a LATER iteration -- a single pass leaves it
      // half-simplified.
      for (int iter = 0; iter < 4; ++iter) {
        PassManager deaggPm(ctx);
        deaggPm.nest<hw::HWModuleOp>().addPass(circt::hw::createHWAggregateToComb());
        deaggPm.addPass(createCanonicalizerPass());
        deaggPm.addPass(createCSEPass());
        if (failed(deaggPm.run(*scratch)))
          return emitError(loc, "post-flatten hw-aggregate-to-comb failed for ")
                 << target.getModuleNameAttr();
      }
      // One more aggregate lowering AFTER the final canonicalize of the
      // loop above: canonicalize itself re-folds constant struct/array
      // fields into hw.aggregate_constant (hw's constant materializer),
      // and an aggregate surviving here fails AIGER export ("unhandled
      // operation"). The loop's iterations each end in canonicalize, so
      // without this the last iteration's folds go un-lowered. Hit on
      // ibex's CHERIoT {CTAG, SD_LM, GL_LG} reset constant once
      // struct-typed reset values started reaching residuals.
      {
        PassManager finalDeaggPm(ctx);
        finalDeaggPm.nest<hw::HWModuleOp>().addPass(
            circt::hw::createHWAggregateToComb());
        if (failed(finalDeaggPm.run(*scratch)))
          return emitError(loc, "final hw-aggregate-to-comb failed for ")
                 << target.getModuleNameAttr();
      }
      legalizeAggregateConstants(cloned);
    }
    // Mirrors CIRCT's own canonical to-AIG pipeline
    // (lib/Dialect/Synth/Transforms/SynthesisPipeline.cpp's
    // buildToSynthPipeline + buildSynthOptimizationPipeline, the relevant
    // subset): AIGER export rejects variadic aig.and_inv gates ("variadic
    // AND gates not supported, run aig-lower-variadic pass first") --
    // ConvertCombToSynth produces them directly from N-ary comb.and/or.
    // LowerWordToBits and LowerVariadic are BOTH `hw::HWModuleOp`-anchored
    // (like HWAggregateToComb above) and need explicit nest<>() the same
    // way -- confirmed the hard way: without nesting, both silently
    // no-op (the PassManager schedules them at the top ModuleOp level,
    // which has no synth.aig ops directly in it -- only nested inside
    // hw.module bodies -- so they report success having done nothing,
    // and large N-ary and_inv chains survive all the way to AIGER
    // export). ConvertCombToSynth itself does not need nesting -- it
    // appears to walk nested modules internally regardless of anchor.
    PassManager pm(ctx);
    if (useDatapath.getValue()) {
      // PHYS-QOR: multi-operand comb.add/sub (and mul partial products) as
      // carry-save compressor trees instead of chained adders. Measured on
      // cv64a6's FP64 FMA cone: 3-operand i164 add 34 -> 22 AIG levels,
      // 5-operand i13 add 30 -> 21, whole cone 281 -> 243.
      pm.addPass(circt::createConvertCombToDatapath());
      if (datapathReduceDelay.getValue())
        pm.nest<hw::HWModuleOp>().addPass(circt::datapath::createDatapathReduceDelay());
      circt::ConvertDatapathToCombOptions dpOpts;
      dpOpts.lowerCompress = true;
      dpOpts.timingAware = datapathTimingAware.getValue();
      dpOpts.forceBooth = datapathForceBooth.getValue();
      pm.addPass(circt::createConvertDatapathToComb(dpOpts));
      pm.addPass(createCSEPass());
      pm.addPass(createCanonicalizerPass());
    }
    pm.addPass(circt::createConvertCombToSynth());
    pm.addPass(createCSEPass());
    pm.addPass(createCanonicalizerPass());
    pm.nest<hw::HWModuleOp>().addPass(circt::synth::createLowerWordToBits());
    pm.addPass(createCSEPass());
    {
      circt::synth::LowerVariadicOptions lowerVariadicOptions;
      lowerVariadicOptions.timingAware = false;
      pm.nest<hw::HWModuleOp>().addPass(
          circt::synth::createLowerVariadic(lowerVariadicOptions));
    }
    pm.addPass(createCanonicalizerPass());
    // Every canonicalize in this pipeline can re-fold constant struct
    // fields back into hw.aggregate_constant (hw's constant materializer)
    // whenever any struct-typed value survives this far -- and one
    // surviving aggregate fails AIGER export as "unhandled operation".
    // Lower aggregates once more as the FINAL structural step, after the
    // last canonicalize. Hit on ibex's CHERIoT {CTAG, SD_LM, GL_LG} reset
    // constant once struct-typed reset values started reaching residuals.
    pm.nest<hw::HWModuleOp>().addPass(circt::hw::createHWAggregateToComb());
    // ... and one more canonicalize purely for DCE: the port-flattening
    // bitcast (flatArg -> struct) goes DEAD once the deagg above converts
    // its last user, but stays in the IR (no pass above removes dead ops
    // after that point) and AIGER export rejects any struct-typed op,
    // live or dead. Safe against re-folding: no live struct-typed op
    // remains for the folder to materialize an aggregate for.
    pm.addPass(createCanonicalizerPass());
    if (failed(pm.run(*scratch)))
      return emitError(loc, "convert-comb-to-synth failed for ")
             << target.getModuleNameAttr();

    auto aigModules = llvm::to_vector(scratch->getOps<hw::HWModuleOp>());
    if (aigModules.size() != 1)
      return emitError(loc, "expected exactly one hw.module after cloning ")
             << target.getModuleNameAttr() << ", got " << aigModules.size();
    hw::HWModuleOp aigModule = aigModules.front();

    // --- 3. Export AIGER. ---
    SmallString<128> aigPath(tempDir);
    llvm::sys::path::append(aigPath, "in.aig");
    {
      auto outFile = mlir::openOutputFile(aigPath);
      if (!outFile)
        return emitError(loc, "failed to open ") << aigPath;
      aiger::ExportAIGEROptions opts;
      opts.binaryFormat = true;
      opts.includeSymbolTable = true;
      if (failed(aiger::exportAIGER(aigModule, outFile->os(), &opts,
                                     /*handler=*/nullptr)))
        return emitError(loc, "AIGER export failed for ")
               << target.getModuleNameAttr();
      outFile->keep();
    }
    if (!saveAigDir.getValue().empty()) {
      SmallString<128> savePath(saveAigDir.getValue());
      if (auto ec = llvm::sys::fs::create_directories(savePath))
        return emitError(loc, "save-aig-dir: cannot create ")
               << savePath << ": " << ec.message();
      llvm::sys::path::append(savePath, (target.getModuleName() + ".aig").str());
      if (auto ec = llvm::sys::fs::copy_file(aigPath, savePath))
        return emitError(loc, "save-aig-dir: cannot write ")
               << savePath << ": " << ec.message();
    }

    // --- 4. Run real ABC. ---
    SmallString<128> mappedVPath(tempDir);
    llvm::sys::path::append(mappedVPath, "mapped.v");
    SmallString<128> abcOutPath = mappedVPath; // ABC's own output location
    bool reuseSaved = !useMappedDir.getValue().empty();
    if (reuseSaved) {
      // Reuse mode: the harness already produced (and possibly re-sized/
      // re-buffered) this target's mapped netlist; skip ABC and re-import
      // it. The file carries the target's own module name (save-mapped-dir
      // wrote it post-rename), and the rename below is name-agnostic, so
      // the identical step-5 path handles it.
      mappedVPath.assign(useMappedDir.getValue());
      llvm::sys::path::append(mappedVPath,
                              (target.getModuleName() + ".v").str());
      if (!llvm::sys::fs::exists(mappedVPath)) {
        if (!useMappedIfPresent.getValue())
          return emitError(loc, "use-mapped-dir: no mapped netlist at ")
                 << mappedVPath;
        // PHYS-DP loop: a group whose logic changed has its saved netlist
        // removed; map it fresh and reuse every other group.
        llvm::errs() << "[abc-bridge] " << target.getModuleName()
                     << ": no saved netlist, mapping fresh\n";
        reuseSaved = false;
        mappedVPath = abcOutPath;
      }
    }
    if (!reuseSaved) {
      // Matches synth.tcl's REAL default recipe -- ABC_HIGH_EFFORT defaults to
      // "1" (synth.tcl line 42; flow.py's own default is likewise True), and
      // nothing in this project's or the reference flow's ibex config overrides
      // it, so the published Yosys baseline numbers (and M1a's own) were
      // produced by synth.tcl's high-effort branch (lines 354-439), NOT the
      // simpler else-branch RECIPE.md originally (incorrectly) transcribed and
      // this script used to match. That branch is: `+strash;scorr;dc2` -> deep
      // AIG restructuring (`&deepsyn`) -> an `&if`-based exploration pass ->
      // resubstitution (`&resub`) -> the final area/timing-balanced `&nf` map
      // -- then synth.tcl separately runs `dfflibmap`/`simplemap` plus two MORE
      // full `abc` invocations, but those exist to finish mapping registers and
      // any leftover generic cells around them; abc-bridge's target is always
      // purely combinational (registers are already real liberty cells via
      // register-mapper before abc-bridge ever runs -- outline-residual-logic's
      // target is "every op except hw.instance"), so only the final cleanup
      // pass (`+&get -n;&nf -D <period>;&put`, synth.tcl line 459) is worth
      // replicating on top, chained in the same ABC process/script since
      // there's no Yosys-level bookkeeping to redo in between.
      // Tier-2 item 10: classical `resub` needs the OLD (non-&) AIG
      // representation -- unlike `&resub`, it has no no-filename-mode stub
      // issue (see the `&resub` note above) and actually performs
      // resubstitution. The existing `&resub -N 2; &put` step already ends in
      // old-AIG space (`&put` converts &-space back to old-space), so this
      // slots in right after it with zero extra strash/&get round-trips beyond
      // what the next pipeline stage already does.
      //
      // Params: a small, non-exhaustive sweep against the CURRENT (post-M1b
      // high-effort) recipe on real ibex_top found ABC's own defaults (`resub`
      // alone, K=8/N=1) and `-K 8 -N 2` both make pre-P&R area slightly WORSE
      // than the baseline -- `-K 16 -N 3` (max cut size, max added-node budget)
      // is the one combination tried that actually helps, by a few hundredths
      // of a percent. This is far smaller than the area win the classical
      // `resub` command found in an earlier ad hoc test (see README.md) -- that
      // test predates the M1b recipe fix (which added `&deepsyn`/`&if`/
      // `&dch`), and most of the headroom classical `resub` used to find has
      // apparently already been captured by that now-higher-effort recipe. `-z`
      // (zero-cost replacements) was also tried on top and made no further
      // difference.
      std::string classicalResubStep =
          enableClassicalResub.getValue() ? "; resub -K 16 -N 3" : "";
      // The optimization body, i.e. everything between read_lib/read_aiger
      // and write_verilog. Kept as a separate string only so
      // --abc-script-file can replace it; with that option empty (the
      // default) the assembled `script` below is byte-for-byte the string
      // this pass has always built.
      // `-t -R <n>` when nf-relax-per-output is set; empty otherwise, which
      // keeps the assembled script byte-identical to the historical one.
      std::string nfReq =
          nfRelaxPerOutput.getValue() >= 0
              ? (" -t -R " + Twine(nfRelaxPerOutput.getValue())).str()
              : std::string();
      std::string body =
          ("strash; scorr; dc2"
           "; &get -n; &deepsyn -J 100 -I 4 -S 200 -T " +
           Twine(deepsynTimeSec.getValue()) + "; &put"
           "; strash; &get -n; &dch -f; &if -C 32 -K 6; &put"
           "; strash; &get -n; &resub -N 2; &put" +
           classicalResubStep +
           "; strash; &get -n; &dch -f; &nf -C 32" + nfReq + " -D " +
           Twine(clockPeriodPs.getValue()) +
           "; &put"
           "; strash; &get -n; &nf" + nfReq + " -D " +
           Twine(clockPeriodPs.getValue()) + "; &put")
              .str();
      if (!abcScriptFile.empty() && failed(readScriptFile(loc, body)))
        return failure();

      // MLIR's pass-option parser splits on whitespace, so the value is
      // passed with commas ("-G,4") and expanded here.
      std::string readLibExtra = readLibArgs.getValue();
      std::replace(readLibExtra.begin(), readLibExtra.end(), ',', ' ');
      std::string readLib = readLibExtra.empty()
                                ? std::string("read_lib ")
                                : ("read_lib " + readLibExtra + " ");
      std::string script = (readLib + libertyPath.getValue() +
                            "; read_aiger " + aigPath.str() + "; " + body +
                            "; write_verilog " + mappedVPath.str())
                               .str();

      if (enableInProcessAbc.getValue()) {
        SmallString<128> abcLogPath(tempDir);
        llvm::sys::path::append(abcLogPath, "abc-stdout.log");
        if (failed(runAbcInProcess(loc, abcShimPath.getValue(), script,
                                    abcLogPath)))
          return failure();
      } else {
        // --- Default path: unchanged subprocess invocation. ---
        auto abcProgram = llvm::sys::findProgramByName(abcPath.getValue());
        if (auto e = abcProgram.getError())
          return emitError(loc, "failed to find ABC binary '")
                 << abcPath.getValue() << "': " << e.message();

        SmallVector<StringRef> args = {*abcProgram, "-q", script};
        std::string execError;
        int rc = llvm::sys::ExecuteAndWait(
            *abcProgram, args, /*Env=*/std::nullopt,
            /*Redirects=*/{}, /*SecondsToWait=*/0,
            /*MemoryLimit=*/0, &execError);
        if (rc != 0)
          return emitError(loc, "ABC invocation failed (rc=")
                 << rc << "): " << execError << "\nscript: " << script;
      }
    }

    // --- 5. Re-import mapped.v (+ blackbox stubs) in-process. ---
    // ABC's `write_verilog` names the module after the AIGER *file path*
    // (its own internal "model name", since our single-line read_aiger
    // script never gave it a real design name) -- e.g. `module
    // \/tmp/abc-bridge-XXXX/in (`, an escaped Verilog identifier -- not
    // `target`'s original symbol name. Rename it before re-import so the
    // reimported module actually matches `target`'s name (confirmed by
    // testing: circt-verilog otherwise refuses it as an invalid top
    // module). ABC's script here only ever emits exactly one module.
    Block anchorBlock2;
    OwningOpRef<ModuleOp> reimport;
    {
      OpBuilder b(ctx);
      b.setInsertionPointToStart(&anchorBlock2);
      reimport = ModuleOp::create(b, loc);
    }
    {
      auto mappedBuf = llvm::MemoryBuffer::getFile(mappedVPath);
      if (!mappedBuf)
        return emitError(loc, "failed to read ABC output: ") << mappedVPath;

      std::string mappedText = (*mappedBuf)->getBuffer().str();
      size_t modKw = mappedText.find("\nmodule ");
      if (modKw == std::string::npos && mappedText.rfind("module ", 0) == 0)
        modKw = 0;
      else if (modKw != std::string::npos)
        ++modKw; // skip the leading '\n'
      if (modKw == std::string::npos)
        return emitError(loc, "ABC output has no 'module' declaration: ")
               << mappedVPath;
      size_t nameStart = modKw + strlen("module ");
      while (nameStart < mappedText.size() && isspace((unsigned char)mappedText[nameStart]))
        ++nameStart;
      // The name ends at whitespace OR at the port list's '(' -- ABC writes
      // `module NAME  ( `, but a writer that emits `module NAME(` with no
      // space (Mockturtle's emap does) would otherwise have its '(' eaten by
      // the replacement below, producing `module NAME \port[0] ...` and a
      // syntax error that points at the port list rather than here.
      size_t nameEnd = nameStart;
      while (nameEnd < mappedText.size() &&
             !isspace((unsigned char)mappedText[nameEnd]) &&
             mappedText[nameEnd] != '(' && mappedText[nameEnd] != ';' &&
             mappedText[nameEnd] != '#')
        ++nameEnd;
      mappedText.replace(nameStart, nameEnd - nameStart,
                          target.getModuleName().str());
      if (llvm::sys::Process::GetEnv("ABC_BRIDGE_KEEP_TEMP"))
        llvm::errs() << "[abc-bridge] renamed module text:\n"
                     << mappedText.substr(0, 400) << "\n---\n";
      if (!saveMappedDir.getValue().empty()) {
        SmallString<128> savePath(saveMappedDir.getValue());
        if (auto ec = llvm::sys::fs::create_directories(savePath))
          return emitError(loc, "save-mapped-dir: cannot create ")
                 << savePath << ": " << ec.message();
        llvm::sys::path::append(savePath,
                                (target.getModuleName() + ".v").str());
        std::error_code ec;
        llvm::raw_fd_ostream os(savePath, ec, llvm::sys::fs::OF_Text);
        if (ec)
          return emitError(loc, "save-mapped-dir: cannot write ")
                 << savePath << ": " << ec.message();
        os << mappedText;
      }
      *mappedBuf = llvm::MemoryBuffer::getMemBufferCopy(mappedText, mappedVPath);

      llvm::SourceMgr sourceMgr;
      sourceMgr.AddNewSourceBuffer(std::move(*mappedBuf), llvm::SMLoc());
      if (!stubsPath.empty()) {
        auto stubsBuf = llvm::MemoryBuffer::getFile(stubsPath.getValue());
        if (!stubsBuf)
          return emitError(loc, "failed to read --stubs file: ")
                 << stubsPath.getValue();
        llvm::StringSet<> wanted = extractInstantiatedCellNames(mappedText);
        std::string filteredStubs =
            extractStubDeclarations((*stubsBuf)->getBuffer(), wanted);
        sourceMgr.AddNewSourceBuffer(
            llvm::MemoryBuffer::getMemBufferCopy(filteredStubs,
                                                  stubsPath.getValue()),
            llvm::SMLoc());
      }

      // Without an attached handler, diagnostics from the Verilog frontend
      // (real parse/elaboration errors, not just crashes) have nowhere to
      // print and are silently dropped -- matches circt-verilog's own tool
      // setup (tools/circt-verilog/circt-verilog.cpp), not optional here.
      mlir::SourceMgrDiagnosticHandler diagHandler(sourceMgr, ctx);

      ImportVerilogOptions opts;
      opts.mode = ImportVerilogOptions::Mode::Full;
      opts.topModules.push_back(target.getModuleName().str());
      mlir::DefaultTimingManager tm;
      mlir::TimingScope ts = tm.getRootScope();
      if (failed(importVerilog(sourceMgr, ctx, ts, *reimport, &opts)))
        return emitError(loc, "failed to reimport ABC's mapped Verilog for ")
               << target.getModuleNameAttr();

      // importVerilog() itself only ever produces Moore-dialect IR
      // regardless of `opts.mode` (confirmed by reading
      // lib/Conversion/ImportVerilog/ImportVerilog.cpp -- `mode` is
      // consulted only for the OnlyLint/OnlyParse early-outs, never
      // branches on Full vs OnlyImport) -- circt-verilog's own tool always
      // runs these two pipelines itself afterward
      // (tools/circt-verilog/circt-verilog.cpp's populatePasses()) to
      // reach HW/Comb. ABC's mapped output is pure structural
      // combinational logic (no processes/registers), so LLHD lowering is
      // not needed here, unlike lower_ibex.sh's frontend path.
      PassManager lowerToCore(ctx);
      populateVerilogToMoorePipeline(lowerToCore);
      populateMooreToCorePipeline(lowerToCore);
      if (failed(lowerToCore.run(*reimport)))
        return emitError(loc, "Moore-to-core lowering failed for reimported ")
               << target.getModuleNameAttr();
    }

    // --- 6. Externalize every cell placeholder module. ---
    externalizeAllExcept(*reimport, target.getModuleName());

    hw::HWModuleOp mappedTarget;
    for (auto m : reimport->getOps<hw::HWModuleOp>())
      if (m.getModuleName() == target.getModuleName())
        mappedTarget = m;
    if (!mappedTarget) {
      llvm::errs() << "[abc-bridge] ops present after reimport:\n";
      for (Operation &op : *reimport->getBody())
        llvm::errs() << "  " << op.getName() << "\n";
      return emitError(loc, "reimported Verilog did not contain module ")
             << target.getModuleNameAttr();
    }

    // --- 7. Splice the mapped body back under the original symbol, and
    //        clone the new extern decls into the real top-level module. ---
    for (auto externOp :
         llvm::make_early_inc_range(reimport->getOps<hw::HWModuleExternOp>())) {
      if (topModule.lookupSymbol(externOp.getModuleName()))
        continue; // already declared (e.g. a previous target in this run)
      OpBuilder b = OpBuilder::atBlockBegin(topModule.getBody());
      b.clone(*externOp);
    }

    // AIGER is inherently bit-level: it has no concept of "these N bits
    // form one word", regardless of what MLIR type the original
    // (pre-export) port had -- confirmed on the real ibex design, where a
    // module with word-level ports (`in0 : i32`, ...) came back from
    // re-import with one SEPARATE i1 port per bit (132 of them for
    // ibex_alu_residual, named after ABC's own escaped-identifier
    // convention `\in0[5] `). Build a thin bridge with `target`'s
    // ORIGINAL (word-level, never-modified) port signature that
    // instantiates the reimported bit-level module (renamed and moved
    // into `topModule` -- it's a REAL, fully-defined module now,
    // containing real liberty cell instances from ABC's mapping, not an
    // extern) and reconstructs word values around it via comb.extract
    // (input side, offset = LSB-based bit index, matching comb.extract's
    // own convention) / comb.concat (output side, MSB-first operand
    // order) -- the exact same per-bit convention already validated in
    // RegisterMapperPass's array/wide-register handling. This relies on
    // mappedTarget's bit-ports preserving `target`'s original port ORDER
    // (all bits of input 0 first, in order, then input 1, etc., then
    // likewise for outputs) end to end through AIGER export -> ABC ->
    // reimport, which holds because every step in between is strictly
    // order-preserving.
    std::string bitsModuleName = (target.getModuleName() + "_bits").str();
    mappedTarget.setName(bitsModuleName);
    // circt-verilog's reimport defaults modules to public visibility (plain
    // Verilog has no "private" concept) -- mark it private so
    // --hw-flatten-modules (run once at the very end of the whole
    // pipeline, see scripts/run_circt_native_synth.sh) actually inlines it
    // into a single flat module instead of leaving it as a separate
    // top-level module, matching synth.tcl's flat single-module output
    // shape for apples-to-apples QoR comparison.
    mappedTarget.setPrivate();
    mappedTarget->remove();
    topModule.getBody()->getOperations().push_front(mappedTarget);

    SmallVector<Type> outputTypes(
        target.getBodyBlock()->getTerminator()->getOperandTypes());

    Block anchorBlock3;
    OwningOpRef<ModuleOp> bridgeScratch;
    hw::HWModuleOp bridgeModule;
    {
      OpBuilder b(ctx);
      b.setInsertionPointToStart(&anchorBlock3);
      bridgeScratch = ModuleOp::create(b, loc);
      OpBuilder bb(ctx);
      bb.setInsertionPointToStart(bridgeScratch->getBody());
      bridgeModule = hw::HWModuleOp::create(bb, loc,
                                             target.getModuleNameAttr(),
                                             target.getPortList());
    }

    OpBuilder bodyB = OpBuilder::atBlockBegin(bridgeModule.getBodyBlock());
    SmallVector<Value> instOperands;
    for (Value arg : bridgeModule.getBodyBlock()->getArguments()) {
      int64_t width = hw::getBitWidth(arg.getType());
      // comb.extract requires an integer operand -- bridgeModule's ports
      // are `target`'s ORIGINAL (possibly aggregate: array/struct) types,
      // unlike mappedTarget's all-i1 bit-ports, so an aggregate-typed
      // argument (e.g. `csr_mcause_i : !hw.struct<...>`) needs bitcasting
      // to a flat integer first.
      Value flatArg = arg;
      if (!isa<IntegerType>(arg.getType()))
        flatArg = hw::BitcastOp::create(bodyB, loc, bodyB.getIntegerType(width), arg);
      for (int64_t i = 0; i < width; ++i)
        instOperands.push_back(
            comb::ExtractOp::create(bodyB, loc, bodyB.getIntegerType(1), flatArg, i));
    }
    auto bitsInst = hw::InstanceOp::create(bodyB, loc, mappedTarget,
                                            bodyB.getStringAttr("u_bits"),
                                            instOperands);

    size_t resultIdx = 0;
    SmallVector<Value> newOutputs;
    for (Type outType : outputTypes) {
      int64_t width = hw::getBitWidth(outType);
      SmallVector<Value> bits;
      for (int64_t i = 0; i < width; ++i)
        bits.push_back(bitsInst.getResult(resultIdx++));
      Value flat;
      if (width == 1) {
        flat = bits[0];
      } else {
        SmallVector<Value> concatOperands(llvm::reverse(bits));
        flat = comb::ConcatOp::create(bodyB, loc, concatOperands);
      }
      // Aggregate-typed outputs (e.g. imd_val_d_o : !hw.array<2xi32>):
      // the extract/concat reconstruction above is always integer-typed,
      // so bitcast back to the module's actual declared output type.
      if (flat.getType() != outType)
        flat = hw::BitcastOp::create(bodyB, loc, outType, flat);
      newOutputs.push_back(flat);
    }
    bridgeModule.getBodyBlock()->getTerminator()->setOperands(newOutputs);

    target.getBody().takeBody(bridgeModule.getBody());
    return success();
  }

  void runOnOperation() override {
    ModuleOp module = getOperation();
    if (libertyPath.empty()) {
      module.emitError("abc-bridge requires --liberty=");
      return signalPassFailure();
    }

    SmallVector<hw::HWModuleOp> targets;
    if (!targetModule.empty()) {
      auto m = module.lookupSymbol<hw::HWModuleOp>(targetModule.getValue());
      if (!m) {
        module.emitError("abc-bridge: no such hw.module: ")
            << targetModule.getValue();
        return signalPassFailure();
      }
      targets.push_back(m);
    } else {
      SmallVector<hw::HWModuleOp> skipped;
      for (auto m : module.getOps<hw::HWModuleOp>()) {
        if (!m.getBodyBlock()->getOps<hw::InstanceOp>().empty())
          continue;
        if (skipUnmappable) {
          // AIGER carries bit-level combinational logic only. Anything still
          // holding an LLHD signal, or an aggregate constant feeding one, has
          // no representation there.
          if (isUnmappableToAiger(m)) {
            skipped.push_back(m);
            continue;
          }
        }
        targets.push_back(m);
      }
      // Turn each skipped module into an extern declaration. Leaving the
      // body in place is not an option downstream: ExportVerilog refuses the
      // whole design on the first LLHD or seq op it meets ("dialect \"llhd\"
      // not supported for direct Verilog emission"), so one unmappable module
      // blocks the netlist entirely. As an extern it becomes a visible,
      // unresolved blackbox instantiation instead -- the incompleteness is
      // still there, but it is legible in the output rather than fatal.
      for (hw::HWModuleOp m : skipped) {
        OpBuilder b(m);
        hw::HWModuleExternOp::create(b, m->getLoc(), m.getModuleNameAttr(),
                                     m.getPortList(), StringRef(),
                                     m.getParameters());
        m.getBodyBlock()->walk([](Operation *op) { op->dropAllReferences(); });
        m.erase();
      }
      if (!skipped.empty())
        llvm::errs() << "[abc-bridge] SKIPPED " << skipped.size()
                     << " unmappable module(s), emitted as blackbox externs"
                     << " -- netlist is INCOMPLETE\n";
    }

    size_t numMapped = 0;
    for (hw::HWModuleOp target : targets) {
      if (failed(runOnModule(target, module)))
        return signalPassFailure();
      ++numMapped;
    }
    llvm::errs() << "[abc-bridge] mapped " << numMapped
                 << " module(s) via real ABC (liberty=" << libertyPath.getValue()
                 << ")\n";

    if (emitTargetOnly) {
      if (targetModule.empty()) {
        module.emitError("abc-bridge: emit-target-only requires target=");
        return signalPassFailure();
      }
      // Keep the target, anything abc-bridge created alongside it (the
      // bit-blasted `_bits` companion it instantiates), and every extern cell
      // declaration. Drop the rest of the design: nothing kept refers to it,
      // so what remains still verifies on its own.
      StringRef keep = targetModule.getValue();
      SmallVector<Operation *> doomed;
      for (auto m : module.getOps<hw::HWModuleOp>()) {
        StringRef name = m.getModuleName();
        if (name == keep || name.starts_with(keep))
          continue;
        doomed.push_back(m);
      }
      for (Operation *op : doomed) {
        op->dropAllReferences();
        op->erase();
      }
    }
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createABCBridgePass() {
  return std::make_unique<ABCBridgePass>();
}

} // namespace circt_abc

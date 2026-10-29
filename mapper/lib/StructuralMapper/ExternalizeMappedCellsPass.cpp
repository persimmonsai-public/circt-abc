//===- ExternalizeMappedCellsPass.cpp ------------------------*- C++ -*-===//
//
// See Passes.td's ExternalizeMappedCells description for the "why". The
// rewrite itself (create hw.module.extern with the same name/ports/params,
// then erase the hw.module) is the exact pattern CIRCT's own circt-reduce
// `ModuleExternalizer` reduction uses (lib/Dialect/HW/HWReductions.cpp) --
// reused here rather than reinvented, just applied unconditionally to a
// caller-supplied name list instead of driven by a reduction search.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/raw_ostream.h"

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

struct ExternalizeMappedCellsPass
    : public PassWrapper<ExternalizeMappedCellsPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ExternalizeMappedCellsPass)

  ExternalizeMappedCellsPass() = default;
  // PassWrapper::clonePass() requires this class to be copy-constructible,
  // but mlir::Pass::Option's own copy constructor is deleted (it holds a
  // back-reference to its owning Pass) -- so the implicitly-defaulted copy
  // constructor here would itself be deleted. Define it explicitly: the
  // member initializer above already re-binds a fresh Option to *this for
  // any constructor that doesn't mention it in its init list, so the body
  // only needs to copy the underlying value.
  ExternalizeMappedCellsPass(const ExternalizeMappedCellsPass &other)
      : PassWrapper(other) {
    cellNamesFile = other.cellNamesFile.getValue();
  }

  StringRef getArgument() const final { return "externalize-mapped-cells"; }
  StringRef getDescription() const final {
    return "Convert hw.module standard-cell placeholders into hw.module.extern";
  }

  Option<std::string> cellNamesFile{
      *this, "cell-names-file",
      llvm::cl::desc("Path to a file listing one standard-cell module name "
                      "per line")};

  void runOnOperation() override {
    ModuleOp module = getOperation();

    if (cellNamesFile.empty()) {
      module.emitError("externalize-mapped-cells requires --cell-names-file");
      return signalPassFailure();
    }

    auto bufferOrErr = llvm::MemoryBuffer::getFile(cellNamesFile);
    if (!bufferOrErr) {
      module.emitError("could not read --cell-names-file: ") << cellNamesFile;
      return signalPassFailure();
    }

    llvm::StringSet<> cellNames;
    for (StringRef line : llvm::split((*bufferOrErr)->getBuffer(), '\n')) {
      line = line.trim();
      if (!line.empty())
        cellNames.insert(line);
    }

    size_t numExternalized = 0;
    for (auto hwModule :
         llvm::make_early_inc_range(module.getOps<hw::HWModuleOp>())) {
      if (!cellNames.contains(hwModule.getModuleName()))
        continue;

      OpBuilder builder(hwModule);
      hw::HWModuleExternOp::create(builder, hwModule->getLoc(),
                                    hwModule.getModuleNameAttr(),
                                    hwModule.getPortList(), StringRef(),
                                    hwModule.getParameters());
      hwModule->erase();
      ++numExternalized;
    }

    llvm::errs() << "[externalize-mapped-cells] converted " << numExternalized
                 << " hw.module -> hw.module.extern\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createExternalizeMappedCellsPass() {
  return std::make_unique<ExternalizeMappedCellsPass>();
}

} // namespace circt_abc

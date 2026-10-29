//===- DatapathAnnotatePass.cpp ----------------------------------*- C++ -*-===//
//
// `--dp-annotate=json=<path>[ min-width=N]`: for every wide arithmetic op
// (add, sub, mul, ordered icmp, variable shifts), emit one JSON record with
// the op's module, kind, width, current `synth.test.arch`, and its LAUNCH
// and CAPTURE anchors -- the registers, module ports and instance pins its
// operand cone comes from and its result cone reaches, found by walking
// comb ops backward/forward inside the module. Registers keep their names
// through the flow (the netlist names flops `<hier>2F<reg>_bit<i>`), so an
// op is identifiable in a placed design by its capture registers' D pins
// (endpoint slack = its timing budget) and their placement (its neighbour-
// hood) WITHOUT any boundary in the logic.
//
// PHYS-DP step 1, second design (2026-09-03): the first design outlined each
// op into its own module for identity and cost ibex measurable area (more in
// its combinational logic) because a bare adder/shifter can no longer share
// with its surroundings (the ALU adder serves add, sub and the ordered
// compares). Annotation costs nothing: the IR is untouched.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/FileSystem.h"
#include <functional>

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

static const char *opKind(Operation *op) {
  if (isa<comb::AddOp>(op)) return "add";
  if (isa<comb::SubOp>(op)) return "sub";
  if (isa<comb::MulOp>(op)) return "mul";
  if (isa<comb::ICmpOp>(op)) return "cmp";
  if (isa<comb::ShlOp>(op)) return "shl";
  if (isa<comb::ShrUOp>(op)) return "shru";
  if (isa<comb::ShrSOp>(op)) return "shrs";
  return nullptr;
}

static bool isOrderedCmp(comb::ICmpOp cmp) {
  switch (cmp.getPredicate()) {
  case comb::ICmpPredicate::slt: case comb::ICmpPredicate::sle:
  case comb::ICmpPredicate::sgt: case comb::ICmpPredicate::sge:
  case comb::ICmpPredicate::ult: case comb::ICmpPredicate::ule:
  case comb::ICmpPredicate::ugt: case comb::ICmpPredicate::uge:
    return true;
  default:
    return false;
  }
}

static bool isCombLike(Operation *op) {
  return isa<comb::CombDialect>(op->getDialect()) ||
         isa<hw::BitcastOp, hw::ConstantOp>(op);
}

struct DatapathAnnotatePass
    : public PassWrapper<DatapathAnnotatePass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(DatapathAnnotatePass)

  DatapathAnnotatePass() = default;
  DatapathAnnotatePass(const DatapathAnnotatePass &o) : PassWrapper(o) {
    jsonPath = o.jsonPath.getValue();
    minWidth = o.minWidth.getValue();
  }

  Option<std::string> jsonPath{*this, "json", llvm::cl::init(""),
                               llvm::cl::desc("sidecar JSON output path")};
  Option<unsigned> minWidth{*this, "min-width", llvm::cl::init(8),
                            llvm::cl::desc("annotate ops at/above this width")};

  StringRef getArgument() const final { return "dp-annotate"; }
  StringRef getDescription() const final {
    return "Emit a sidecar JSON of wide arithmetic ops with their launch/"
           "capture register anchors (identity without boundaries)";
  }

  // module name -> its instantiation sites (parent module, instance op)
  llvm::DenseMap<StringAttr, SmallVector<std::pair<hw::HWModuleOp, hw::InstanceOp>, 2>> sites;
  llvm::DenseMap<StringAttr, hw::HWModuleOp> modules;
  unsigned crossings = 0;
  static constexpr unsigned kMaxAnchors = 64;

  hw::HWModuleOp bodyOf(hw::InstanceOp inst) {
    auto it = modules.find(inst.getModuleNameAttr().getAttr());
    return it == modules.end() ? hw::HWModuleOp() : it->second;
  }

  // A frame: the current module, the path of instance names we DESCENDED
  // through from the op's module (so an ascent pops it), and the path of
  // instance names we ASCENDED through from the op's module (rendered as
  // "^inst/" so the netlist prefix can be reconstructed by the consumer).
  struct Frame {
    hw::HWModuleOp mod;
    SmallVector<std::string, 4> down;
    SmallVector<std::string, 4> up;
  };
  std::string render(const Frame &f, StringRef leaf) const {
    std::string r;
    for (auto &u : llvm::reverse(f.up)) r += "^" + u + "/";
    for (auto &d : f.down) r += d + "/";
    return r + leaf.str();
  }
  struct Collector {
    llvm::StringSet<> seen;
    llvm::json::Array &out;
    unsigned dropped = 0;
    Collector(llvm::json::Array &o) : out(o) {}
    void add(std::string a) {
      if (!seen.insert(a).second) return;
      if (out.size() < kMaxAnchors) out.push_back(a); else ++dropped;
    }
  };

  void launchAnchors(Value v, Collector &c, SmallPtrSetImpl<Value> &seen,
                     const Frame &f, unsigned depth) {
    if (!seen.insert(v).second || depth > 200)
      return;
    if (auto arg = dyn_cast<BlockArgument>(v)) {
      unsigned idx = arg.getArgNumber();
      if (!f.down.empty()) {
        // we descended into this module through a known instance: pop
        Frame g = f; std::string inst = g.down.pop_back_val();
        auto parent = g.mod = parentOf(f, inst);
        if (!parent) return;
        hw::InstanceOp io = findInstance(parent, inst);
        ++crossings;
        launchAnchors(io.getOperand(idx), c, seen, g, depth + 1);
        return;
      }
      hw::HWModuleOp cur = f.mod;
      auto it = sites.find(cur.getModuleNameAttr());
      if (it == sites.end()) { c.add("port:" + render(f, cur.getInputName(idx))); return; }
      for (auto &[parent, inst] : it->second) {
        Frame g = f; g.mod = parent; g.up.push_back(inst.getInstanceName().str());
        ++crossings;
        launchAnchors(inst.getOperand(idx), c, seen, g, depth + 1);
      }
      return;
    }
    Operation *def = v.getDefiningOp();
    if (!def) return;
    if (auto reg = dyn_cast<seq::FirRegOp>(def)) { c.add("reg:" + render(f, reg.getName())); return; }
    if (auto inst = dyn_cast<hw::InstanceOp>(def)) {
      unsigned r = cast<OpResult>(v).getResultNumber();
      if (auto child = bodyOf(inst)) {
        Frame g = f; g.mod = child; g.down.push_back(inst.getInstanceName().str());
        auto outOp = cast<hw::OutputOp>(child.getBodyBlock()->getTerminator());
        ++crossings;
        launchAnchors(outOp.getOperand(r), c, seen, g, depth + 1);
      } else {
        c.add("inst:" + render(f, inst.getInstanceName()) + "." +
              cast<StringAttr>(inst.getResultNames()[r]).getValue().str());
      }
      return;
    }
    if (isCombLike(def))
      for (Value o : def->getOperands())
        launchAnchors(o, c, seen, f, depth + 1);
    else
      c.add("op:" + def->getName().getStringRef().str());
  }

  void captureAnchors(Value v, Collector &c, SmallPtrSetImpl<Value> &seen,
                      const Frame &f, unsigned depth) {
    if (!seen.insert(v).second || depth > 200)
      return;
    for (OpOperand &use : v.getUses()) {
      Operation *user = use.getOwner();
      if (auto reg = dyn_cast<seq::FirRegOp>(user)) {
        c.add("reg:" + render(f, reg.getName()));
      } else if (isa<hw::OutputOp>(user)) {
        unsigned idx = use.getOperandNumber();
        if (!f.down.empty()) {
          Frame g = f; std::string inst = g.down.pop_back_val();
          auto parent = g.mod = parentOf(f, inst);
          if (!parent) continue;
          hw::InstanceOp io = findInstance(parent, inst);
          ++crossings;
          captureAnchors(io.getResult(idx), c, seen, g, depth + 1);
          continue;
        }
        hw::HWModuleOp cur = f.mod;
        auto it = sites.find(cur.getModuleNameAttr());
        if (it == sites.end()) { c.add("port:" + render(f, cur.getOutputName(idx))); continue; }
        for (auto &[parent, inst] : it->second) {
          Frame g = f; g.mod = parent; g.up.push_back(inst.getInstanceName().str());
          ++crossings;
          captureAnchors(inst.getResult(idx), c, seen, g, depth + 1);
        }
      } else if (auto inst = dyn_cast<hw::InstanceOp>(user)) {
        unsigned idx = use.getOperandNumber();
        if (auto child = bodyOf(inst)) {
          Frame g = f; g.mod = child; g.down.push_back(inst.getInstanceName().str());
          ++crossings;
          captureAnchors(child.getBodyBlock()->getArgument(idx), c, seen, g, depth + 1);
        } else {
          c.add("inst:" + render(f, inst.getInstanceName()) + "." +
                cast<StringAttr>(inst.getArgNames()[idx]).getValue().str());
        }
      } else if (isCombLike(user)) {
        for (Value r : user->getResults())
          captureAnchors(r, c, seen, f, depth + 1);
      } else {
        c.add("op:" + user->getName().getStringRef().str());
      }
    }
  }

  // the module we descended FROM is recoverable from the frame's own chain:
  // walking down pushes instance names; the parent of the current module for
  // a pop is the module that holds that instance -- found through `sites`.
  hw::HWModuleOp parentOf(const Frame &f, StringRef instName) {
    hw::HWModuleOp cur = f.mod;
    auto it = sites.find(cur.getModuleNameAttr());
    if (it == sites.end()) return {};
    for (auto &[parent, inst] : it->second)
      if (inst.getInstanceName() == instName) return parent;
    return {};
  }
  hw::InstanceOp findInstance(hw::HWModuleOp parent, StringRef instName) {
    hw::InstanceOp found;
    parent.walk([&](hw::InstanceOp i) { if (i.getInstanceName() == instName) found = i; });
    return found;
  }

  void runOnOperation() override {
    mlir::ModuleOp top = getOperation();
    llvm::json::Array records;
    for (auto mod : top.getOps<hw::HWModuleOp>())
      modules[mod.getModuleNameAttr()] = mod;
    for (auto mod : top.getOps<hw::HWModuleOp>())
      mod.walk([&](hw::InstanceOp inst) {
        if (modules.count(inst.getModuleNameAttr().getAttr()))
          sites[inst.getModuleNameAttr().getAttr()].push_back({mod, inst});
      });
    // full instance paths of every module from the roots (modules with no
    // sites), so a consumer can turn frame-relative anchors into exact
    // netlist names (<path>2F<flop>)
    llvm::DenseMap<StringAttr, SmallVector<std::string, 2>> paths;
    std::function<void(hw::HWModuleOp, const std::string &, unsigned)> dfs =
        [&](hw::HWModuleOp m, const std::string &pfx, unsigned depth) {
          if (depth > 64) return;
          paths[m.getModuleNameAttr()].push_back(pfx);
          m.walk([&](hw::InstanceOp inst) {
            if (auto child = bodyOf(inst))
              dfs(child, pfx + inst.getInstanceName().str() + "/", depth + 1);
          });
        };
    for (auto mod : top.getOps<hw::HWModuleOp>())
      if (!sites.count(mod.getModuleNameAttr()))
        dfs(mod, "", 0);
    for (auto mod : top.getOps<hw::HWModuleOp>()) {
      unsigned k = 0;
      mod.walk([&](Operation *op) {
        const char *kind = opKind(op);
        if (!kind || op->getNumResults() != 1 || op->getNumOperands() < 2)
          return;
        if (isa<comb::ShlOp, comb::ShrUOp, comb::ShrSOp>(op) &&
            op->getOperand(1).getDefiningOp<hw::ConstantOp>())
          return;
        if (auto cmp = dyn_cast<comb::ICmpOp>(op))
          if (!isOrderedCmp(cmp))
            return;
        auto ty = dyn_cast<IntegerType>(op->getOperand(0).getType());
        if (!ty || ty.getWidth() < minWidth.getValue())
          return;
        bool constOperand = false;
        for (Value v : op->getOperands())
          if (v.getDefiningOp<hw::ConstantOp>())
            constOperand = true;
        llvm::json::Object rec;
        rec["module"] = mod.getModuleName().str();
        rec["id"] = (mod.getModuleName() + "_dp" + Twine(k)).str();
        rec["op"] = kind;
        rec["width"] = (int64_t)ty.getWidth();
        rec["const_operand"] = constOperand;
        // effective width: the widest operand counted without its constant
        // bits (a zero-extension or a constant operand contributes nothing);
        // the chooser's depth model must not credit constant bits (cv64a6:
        // `2 - zext(1 bit)` was costed as a full "32-bit sub" on the table)
        {
          int64_t eff = 0;
          for (Value v : op->getOperands()) {
            int64_t w = 0;
            if (v.getDefiningOp<hw::ConstantOp>())
              w = 0;
            else if (auto cc = v.getDefiningOp<comb::ConcatOp>()) {
              for (Value e : cc.getInputs())
                if (!e.getDefiningOp<hw::ConstantOp>())
                  if (auto et = dyn_cast<IntegerType>(e.getType()))
                    w += et.getWidth();
            } else if (auto vt = dyn_cast<IntegerType>(v.getType()))
              w = vt.getWidth();
            eff = std::max(eff, w);
          }
          rec["eff_width"] = eff;
        }
        if (auto a = op->getAttrOfType<StringAttr>("synth.test.arch"))
          rec["arch"] = a.getValue().str();
        llvm::json::Array launch, capture;
        Collector cl(launch), cc(capture);
        SmallPtrSet<Value, 32> seenL, seenC;
        Frame f{mod, {}, {}};
        for (Value o : op->getOperands())
          launchAnchors(o, cl, seenL, f, 0);
        captureAnchors(op->getResult(0), cc, seenC, f, 0);
        {
          llvm::json::Array ps;
          for (auto &pp : paths[mod.getModuleNameAttr()]) ps.push_back(pp);
          rec["paths"] = std::move(ps);
        }
        rec["launch_total"] = (int64_t)(launch.size() + cl.dropped);
        rec["capture_total"] = (int64_t)(capture.size() + cc.dropped);
        rec["launch"] = std::move(launch);
        rec["capture"] = std::move(capture);
        records.push_back(std::move(rec));
        ++k;
      });
    }
    if (jsonPath.getValue().empty()) {
      llvm::errs() << "[dp-annotate] " << records.size() << " wide op(s), " << crossings << " boundary crossing(s)\n";
      return;
    }
    std::error_code ec;
    llvm::raw_fd_ostream os(jsonPath.getValue(), ec, llvm::sys::fs::OF_Text);
    if (ec) {
      top.emitError("dp-annotate: cannot write ") << jsonPath.getValue();
      return signalPassFailure();
    }
    os << llvm::json::Value(std::move(records)) << "\n";
    llvm::errs() << "[dp-annotate] wrote " << jsonPath.getValue() << "\n";
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>>
createDatapathAnnotatePass() {
  return std::make_unique<DatapathAnnotatePass>();
}

} // namespace circt_abc

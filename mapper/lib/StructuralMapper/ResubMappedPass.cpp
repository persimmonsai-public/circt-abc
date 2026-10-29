//===- ResubMappedPass.cpp -----------------------------------*- C++ -*-===//
//
// `--resub-mapped`: technology-aware resubstitution ON THE MAPPED NETLIST.
//
// An implementation of the scalable core of "Area-Oriented Optimization
// After Standard-Cell Mapping" (Costamagna, Tempia Calvino, Mishchenko,
// De Micheli, ASP-DAC 2025), section 3.3 "Structural Network
// Exploration", which that paper itself says "should be preferred due to
// its scalability":
//
//   represent the mapped network as nodes carrying a library gate id;
//   enumerate structural cuts over THAT network; for each cut extract the
//   cut functionality; look the function up in a database of mapped
//   sub-networks; and "if the area of the stored network is smaller than
//   the area of the sub-circuit contained in the cut, perform the
//   substitution."
//
// WHY THIS FINDS ANYTHING THE MAPPER MISSED. The mapper enumerates cuts
// of the AIG and covers it greedily; the netlist it emits then inherits
// that covering. The paper names this *structural bias*. Cuts of the
// MAPPED graph group logic along different boundaries than the AIG cuts
// did, so a two- or three-cell sub-circuit here can turn out to be one
// library cell -- a match the mapper never had the opportunity to see.
//
// DIFFERENCES FROM THE PAPER, stated plainly:
//   * Database. The paper precomputes an area-optimal MAPPED netlist for
//     each of the 3,984 P-classes of 4-input functions. This uses
//     LibertyLibrary::lookup() instead -- the cheapest SINGLE cell
//     realising the function under any input permutation. That is a
//     database of one-cell netlists: strictly weaker (it can never
//     propose a cheaper multi-cell structure), but sound, and it needs no
//     database generation step. Extending to k-cell entries is the
//     obvious next increment.
//   * Don't cares (paper 3.2) are NOT used. Every substitution here is
//     exact on the completely specified cut function.
//   * Dependency cuts / divisors (paper 3.4), the non-local half, are not
//     implemented. Only structural cuts, i.e. the transitive fanin.
// So this should be expected to recover part of the paper's 5.47%, not
// all of it.
//
// Guards: single-output cells only, i1 ports only, no inner symbols, a
// cut is rejected unless every node in it resolves to a liberty cell, and
// a substitution is taken only when it STRICTLY reduces area (an inverted
// match pays for its inverter first). The rewrite is exact by
// construction -- the new cell computes the cut function the old
// sub-circuit computed -- but it is a netlist change, so it lands OFF by
// default per the project convention and must be CEC-gated like any
// other.
//
//===----------------------------------------------------------------------===//

#include "circt-abc/Passes.h"

#include "LibertyLibrary.h"

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/BuiltinOps.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/Format.h"
#include "llvm/Support/raw_ostream.h"

#include <algorithm>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

using namespace mlir;
using namespace circt;

namespace circt_abc {
namespace {

/// One node of the mapped graph: a single-output cell instance.
struct MNode {
  hw::InstanceOp op;
  // Cached raw pointer: the helpers below take `ArrayRef<MNode>`, and
  // InstanceOp::getOperation() is non-const.
  Operation *raw = nullptr;
  const CellPattern *cell = nullptr;
  SmallVector<Value, 6> ins;
  Value out;
};

/// One cell of a stored database sub-network. `srcs[i]` is the source for
/// the cell's i-th LIBERTY input pin: >=0 means database leaf L<srcs[i]>,
/// and -1-k means the k-th earlier cell of the same entry.
struct DbCell {
  std::string name;
  SmallVector<int, 6> srcs;
};

/// A database entry: an area-optimal mapped netlist for one P-class
/// representative of a 4-input function (paper section 3.1). The LAST
/// cell is the entry's output.
struct DbEntry {
  double area = 0.0;
  SmallVector<DbCell, 8> cells;
  unsigned maxLeaf = 0; // highest leaf index actually referenced
};

/// Permute a 4-variable truth table: new variable `j` takes the value of
/// original variable `perm[j]`.
static TruthTable permute4(TruthTable tt, const unsigned *perm) {
  TruthTable out = 0;
  for (unsigned m = 0; m < 16; ++m) {
    unsigned om = 0;
    for (unsigned j = 0; j < 4; ++j)
      if ((m >> j) & 1)
        om |= 1u << perm[j];
    if ((tt >> om) & 1)
      out |= (static_cast<TruthTable>(1) << m);
  }
  return out;
}

/// A structural cut: the set of leaf values feeding a cone.
using Cut = SmallVector<Value, 6>;

static bool cutContains(const Cut &c, Value v) {
  return llvm::is_contained(c, v);
}

/// Merge two cuts, failing if the union exceeds `k` leaves.
static bool mergeCuts(const Cut &a, const Cut &b, unsigned k, Cut &out) {
  out.assign(a.begin(), a.end());
  for (Value v : b) {
    if (cutContains(out, v))
      continue;
    if (out.size() >= k)
      return false;
    out.push_back(v);
  }
  return true;
}

struct ResubMappedPass
    : public PassWrapper<ResubMappedPass, OperationPass<mlir::ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ResubMappedPass)

  ResubMappedPass() = default;
  // Same explicit-copy-ctor requirement as every other hand-rolled
  // PassWrapper in this project (see ABCBridge.cpp's header comment).
  ResubMappedPass(const ResubMappedPass &other) : PassWrapper(other) {
    libertyPath = other.libertyPath.getValue();
    enableResub = other.enableResub.getValue();
    cutSize = other.cutSize.getValue();
    cutLimit = other.cutLimit.getValue();
    maxRounds = other.maxRounds.getValue();
  }

  StringRef getArgument() const final { return "resub-mapped"; }
  StringRef getDescription() const final {
    return "Technology-aware resubstitution on the mapped netlist: "
           "enumerate structural cuts over library cells and replace a "
           "sub-circuit by a cheaper single cell computing the same "
           "function (ASP-DAC'25 section 3.3)";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<hw::HWDialect, comb::CombDialect>();
  }

  Option<std::string> libertyPath{
      *this, "liberty",
      llvm::cl::desc("Liberty file the netlist was mapped against")};
  Option<bool> enableResub{
      *this, "enable-resub-mapped",
      llvm::cl::desc("Enable post-mapping resubstitution. OFF by default "
                     "per the project convention."),
      llvm::cl::init(false)};
  Option<unsigned> cutSize{
      *this, "cut-size",
      llvm::cl::desc("Max cut leaves (<=6, the LibertyLibrary truth-table "
                     "cap). The paper uses 4."),
      llvm::cl::init(4)};
  Option<unsigned> cutLimit{
      *this, "cut-limit",
      llvm::cl::desc("Max cuts kept per node during enumeration"),
      llvm::cl::init(8)};
  Option<std::string> databasePath{
      *this, "database",
      llvm::cl::desc("Database of area-optimal mapped sub-networks, one per "
                     "P-class of 4-input functions (paper section 3.1). "
                     "Generated by mapping every P-representative with ABC. "
                     "Without it the pass falls back to single-cell matches "
                     "only, which recovers almost nothing.")};

  Option<unsigned> maxRounds{
      *this, "max-rounds",
      llvm::cl::desc("Repeat until no substitution is found, at most N times"),
      llvm::cl::init(3)};

  void runOnOperation() override {
    if (!enableResub.getValue())
      return;
    if (libertyPath.getValue().empty()) {
      getOperation().emitError("resub-mapped: liberty= is required when "
                               "enable-resub-mapped is set");
      return signalPassFailure();
    }
    auto lib = LibertyLibrary::loadFromFile(libertyPath.getValue());
    if (!lib) {
      getOperation().emitError("resub-mapped: failed to load liberty file ")
          << libertyPath.getValue();
      return signalPassFailure();
    }
    libRef = &*lib;
    // Load the database if one was given.
    if (!databasePath.getValue().empty() && !loadDatabase())
      return signalPassFailure();

    unsigned k = std::min<unsigned>(cutSize.getValue(), 6);
    if (k < 2)
      return;

    mlir::ModuleOp top = getOperation();
    unsigned totalSubs = 0;
    double totalSaved = 0.0;
    for (unsigned round = 0; round < maxRounds.getValue(); ++round) {
      unsigned subs = 0;
      double saved = 0.0;
      for (auto hwmod : top.getOps<hw::HWModuleOp>())
        subs += runOnModule(hwmod, *lib, k, saved);
      totalSubs += subs;
      totalSaved += saved;
      if (subs == 0)
        break;
    }
    if (totalSubs > 0)
      llvm::errs() << "[resub-mapped] " << totalSubs
                   << " substitution(s), " << llvm::format("%.2f", totalSaved)
                   << " um^2 saved\n";
  }

  llvm::DenseMap<uint32_t, DbEntry> db;
  const LibertyLibrary *libRef = nullptr;

  /// Parse the database emitted by the generator script. Lines are
  ///   TT <hex4> <area> <ncells>
  ///   C <cellname> <PIN>=<L<k>|N<k>> ...
  /// with the last C line of an entry being that entry's output.
  bool loadDatabase() {
    std::ifstream in(databasePath.getValue());
    if (!in) {
      getOperation().emitError("resub-mapped: cannot open database ")
          << databasePath.getValue();
      return false;
    }
    std::string line;
    uint32_t curTT = 0;
    bool have = false;
    while (std::getline(in, line)) {
      std::istringstream ls(line);
      std::string tag;
      if (!(ls >> tag))
        continue;
      if (tag == "TT") {
        std::string hex;
        double area;
        unsigned n;
        if (!(ls >> hex >> area >> n))
          continue;
        curTT = static_cast<uint32_t>(std::stoul(hex, nullptr, 16));
        DbEntry e;
        e.area = area;
        db[curTT] = std::move(e);
        have = true;
      } else if (tag == "C" && have) {
        std::string cellName, tok;
        if (!(ls >> cellName))
          continue;
        // Pin assignments are name=source; resolve to the cell's liberty
        // input-pin ORDER so the pass can instantiate positionally.
        const CellPattern *pat = libRef->patternFor(cellName);
        if (!pat) {
          db.erase(curTT);
          have = false;
          continue;
        }
        DbCell c;
        c.name = cellName;
        c.srcs.assign(pat->arity(), 0);
        SmallVector<bool, 6> seen(pat->arity(), false);
        bool ok = true;
        while (ls >> tok) {
          auto eq = tok.find('=');
          if (eq == std::string::npos) { ok = false; break; }
          std::string pin = tok.substr(0, eq), src = tok.substr(eq + 1);
          unsigned slot = pat->arity();
          for (unsigned i = 0; i < pat->arity(); ++i)
            if (pat->inputPins[i] == pin) { slot = i; break; }
          if (slot == pat->arity() || src.size() < 2) { ok = false; break; }
          int v = std::stoi(src.substr(1));
          if (src[0] == 'L') {
            c.srcs[slot] = v;
            db[curTT].maxLeaf = std::max(db[curTT].maxLeaf, (unsigned)v);
          } else if (src[0] == 'N') {
            c.srcs[slot] = -1 - v;
          } else { ok = false; break; }
          seen[slot] = true;
        }
        for (bool b : seen)
          if (!b) ok = false;
        if (!ok) { db.erase(curTT); have = false; continue; }
        db[curTT].cells.push_back(std::move(c));
      }
    }
    llvm::errs() << "[resub-mapped] database: " << db.size()
                 << " P-class entries\n";
    return true;
  }

  /// Returns the number of substitutions made in `hwmod`.
  unsigned runOnModule(hw::HWModuleOp hwmod, const LibertyLibrary &lib,
                       unsigned k, double &saved) {
    MLIRContext *ctx = hwmod.getContext();
    Type i1 = IntegerType::get(ctx, 1);

    // Cell externs actually declared in this design, so an ordinary
    // hw.module sharing a cell's name can never be mistaken for one.
    auto top = hwmod->getParentOfType<mlir::ModuleOp>();
    llvm::DenseSet<StringRef> declared;
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      declared.insert(ext.getModuleName());

    // Build the mapped graph in block order, which is already topological
    // (SSA dominance).
    llvm::DenseMap<Value, unsigned> nodeOf;
    SmallVector<MNode> nodes;
    for (auto inst : hwmod.getBodyBlock()->getOps<hw::InstanceOp>()) {
      if (inst.getNumResults() != 1 || inst.getInnerSymAttr())
        continue;
      if (inst.getResult(0).getType() != i1)
        continue;
      if (!declared.contains(inst.getModuleName()))
        continue;
      const CellPattern *cell = lib.patternFor(inst.getModuleName());
      if (!cell || cell->arity() != inst.getNumOperands() ||
          cell->arity() < 1 || cell->arity() > 6)
        continue;
      bool ok = true;
      for (Value v : inst.getOperands())
        if (v.getType() != i1)
          ok = false;
      if (!ok)
        continue;
      MNode n;
      n.op = inst;
      n.raw = inst.getOperation();
      n.cell = cell;
      n.ins.assign(inst.getOperands().begin(), inst.getOperands().end());
      n.out = inst.getResult(0);
      nodeOf[n.out] = nodes.size();
      nodes.push_back(std::move(n));
    }
    if (nodes.empty())
      return 0;

    // Cut enumeration over the mapped graph. cuts[i] holds the kept cuts
    // of node i, always including the trivial cut {out_i}.
    std::vector<SmallVector<Cut, 8>> cuts(nodes.size());
    for (unsigned i = 0; i < nodes.size(); ++i) {
      SmallVector<Cut, 8> &cs = cuts[i];
      // Start from the cross product of the fanins' cuts.
      SmallVector<Cut, 8> acc;
      acc.push_back(Cut{});
      for (Value in : nodes[i].ins) {
        SmallVector<Cut, 8> next;
        auto it = nodeOf.find(in);
        SmallVector<Cut, 8> inCuts;
        if (it == nodeOf.end()) {
          inCuts.push_back(Cut{in}); // a leaf of the mapped graph
        } else {
          inCuts = cuts[it->second];
        }
        for (const Cut &a : acc) {
          for (const Cut &b : inCuts) {
            Cut m;
            if (!mergeCuts(a, b, k, m))
              continue;
            // Dedup on the leaf set.
            bool dup = false;
            for (const Cut &e : next)
              if (e.size() == m.size() &&
                  std::all_of(m.begin(), m.end(),
                              [&](Value v) { return cutContains(e, v); })) {
                dup = true;
                break;
              }
            if (!dup)
              next.push_back(std::move(m));
            if (next.size() >= cutLimit.getValue())
              break;
          }
          if (next.size() >= cutLimit.getValue())
            break;
        }
        acc = std::move(next);
        if (acc.empty())
          break;
      }
      for (Cut &c : acc)
        if (!c.empty())
          cs.push_back(std::move(c));
      cs.push_back(Cut{nodes[i].out}); // trivial cut, always available
      if (cs.size() > cutLimit.getValue())
        cs.resize(cutLimit.getValue());
    }

    // Try each node's non-trivial cuts, largest cone first.
    unsigned made = 0;
    llvm::DenseSet<Operation *> dead;
    for (unsigned i = 0; i < nodes.size(); ++i) {
      if (dead.contains(nodes[i].op.getOperation()))
        continue;
      for (const Cut &cut : cuts[i]) {
        if (cut.size() < 2 || cut.size() > k)
          continue;
        if (cut.size() == 1 && cut[0] == nodes[i].out)
          continue;

        // Cone = nodes reachable from i, stopping at the cut leaves.
        SmallVector<unsigned> cone;
        if (!collectCone(i, cut, nodes, nodeOf, cone, dead))
          continue;
        if (cone.size() < 2)
          continue; // one cell is already the best a one-cell database can do

        TruthTable tt;
        if (!computeCutFunction(i, cut, cone, nodes, nodeOf, tt))
          continue;

        // Area of what we would remove: the cone nodes whose every use
        // is inside the cone (the MFFC).
        double oldArea = 0.0;
        SmallVector<unsigned> mffc;
        computeMffc(i, cone, nodes, nodeOf, dead, mffc, oldArea);
        if (mffc.empty())
          continue;

        OpBuilder b(nodes[i].op);
        b.setInsertionPoint(nodes[i].op);
        Value repl;
        double newArea = 0.0;

        // --- Database path (the paper's mechanism) -------------------
        // Extend the cut function to 4 variables, take its P-class
        // representative, and look up the stored area-optimal mapped
        // sub-network. This is what a one-cell match cannot do: propose a
        // cheaper MULTI-cell structure.
        if (!db.empty() && cut.size() <= 4) {
          TruthTable tt4 = tt;
          for (unsigned nv = cut.size(); nv < 4; ++nv)
            tt4 |= tt4 << (1u << nv);
          tt4 &= 0xFFFFu;
          static const unsigned kPerms[24][4] = {
              {0,1,2,3},{0,1,3,2},{0,2,1,3},{0,2,3,1},{0,3,1,2},{0,3,2,1},
              {1,0,2,3},{1,0,3,2},{1,2,0,3},{1,2,3,0},{1,3,0,2},{1,3,2,0},
              {2,0,1,3},{2,0,3,1},{2,1,0,3},{2,1,3,0},{2,3,0,1},{2,3,1,0},
              {3,0,1,2},{3,0,2,1},{3,1,0,2},{3,1,2,0},{3,2,0,1},{3,2,1,0}};
          TruthTable best = ~(TruthTable)0;
          const unsigned *bestPerm = kPerms[0];
          for (const auto &pm : kPerms) {
            TruthTable c = permute4(tt4, pm);
            if (c < best) { best = c; bestPerm = pm; }
          }
          auto it = db.find(static_cast<uint32_t>(best));
          if (it != db.end() && it->second.maxLeaf < cut.size() &&
              it->second.area < oldArea) {
            const DbEntry &e = it->second;
            SmallVector<Value, 8> made2;
            bool ok = true;
            for (const DbCell &dc : e.cells) {
              const CellPattern *pat = lib.patternFor(dc.name);
              if (!pat) { ok = false; break; }
              SmallVector<Value, 6> args;
              for (int src : dc.srcs) {
                if (src >= 0) {
                  // Database leaf j is wired to cut leaf bestPerm[j].
                  args.push_back(cut[bestPerm[src]]);
                } else {
                  unsigned idx = (unsigned)(-1 - src);
                  if (idx >= made2.size()) { ok = false; break; }
                  args.push_back(made2[idx]);
                }
              }
              if (!ok) break;
              auto ext = lookupExtern(top, pat, i1);
              auto ni = hw::InstanceOp::create(
                  b, nodes[i].op.getLoc(), ext,
                  b.getStringAttr("rdb_" + std::to_string(made) + "_" +
                                  std::to_string(made2.size())),
                  args);
              made2.push_back(ni.getResult(0));
            }
            if (ok && !made2.empty()) {
              repl = made2.back();
              newArea = e.area;
            } else {
              // Roll back a partially built structure, innermost last, or
              // it leaks into the netlist: those orphans are not in `dead`
              // and nothing else would ever remove them.
              for (unsigned q = made2.size(); q-- > 0;)
                if (made2[q].use_empty())
                  made2[q].getDefiningOp()->erase();
            }
          }
        }

        // --- Single-cell fallback ------------------------------------
        if (!repl) {
          auto match = lib.lookup(cut.size(), tt);
          if (!match)
            continue;
          double cand = match->cell->area;
          const CellPattern *invCell = nullptr;
          if (match->inverted) {
            auto invMatch = lib.lookup(1, /*table for !a =*/1);
            if (!invMatch || invMatch->inverted)
              continue;
            invCell = invMatch->cell;
            cand += invCell->area;
          }
          if (cand >= oldArea)
            continue;
          SmallVector<Value, 6> args(match->cell->arity());
          for (unsigned slot = 0; slot < match->cell->arity(); ++slot)
            args[slot] = cut[match->leafForCellInputSlot[slot]];
          auto ext = lookupExtern(top, match->cell, i1);
          auto nu = hw::InstanceOp::create(
              b, nodes[i].op.getLoc(), ext,
              b.getStringAttr("rsb_" + std::to_string(made)), args);
          repl = nu.getResult(0);
          if (invCell) {
            auto invExt = lookupExtern(top, invCell, i1);
            auto invInst = hw::InstanceOp::create(
                b, nodes[i].op.getLoc(), invExt,
                b.getStringAttr("rsbi_" + std::to_string(made)),
                ArrayRef<Value>{repl});
            repl = invInst.getResult(0);
          }
          newArea = cand;
        }

        nodes[i].out.replaceAllUsesWith(repl);
        for (unsigned idx : mffc)
          dead.insert(nodes[idx].raw);
        saved += oldArea - newArea;
        ++made;
        break; // this node is done
      }
    }

    // Erase what became unused, innermost first. Also sweeps any cell
    // instance that ended up with no users at all -- a substitution that
    // superseded an earlier one can orphan cells that were never in
    // `dead`, and leaving them behind silently grows the netlist while the
    // per-substitution accounting still reports a win.
    for (auto inst : llvm::make_early_inc_range(
             hwmod.getBodyBlock()->getOps<hw::InstanceOp>())) {
      if (inst.getNumResults() == 1 && inst.getResult(0).use_empty() &&
          !inst.getInnerSymAttr() && declared.contains(inst.getModuleName()) &&
          lib.patternFor(inst.getModuleName()))
        dead.insert(inst.getOperation());
    }
    bool changed = true;
    while (changed) {
      changed = false;
      SmallVector<Operation *> erasable;
      for (Operation *op : dead)
        if (op->use_empty())
          erasable.push_back(op);
      for (Operation *op : erasable) {
        dead.erase(op);
        op->erase();
        changed = true;
      }
    }
    return made;
  }

  /// Collect the cone of node `root` down to `cut`'s leaves. Returns false
  /// if the cone touches a node already scheduled for deletion.
  bool collectCone(unsigned root, const Cut &cut, ArrayRef<MNode> nodes,
                   const llvm::DenseMap<Value, unsigned> &nodeOf,
                   SmallVector<unsigned> &cone,
                   const llvm::DenseSet<Operation *> &dead) {
    llvm::DenseSet<unsigned> seen;
    SmallVector<unsigned> stack{root};
    while (!stack.empty()) {
      unsigned n = stack.pop_back_val();
      if (!seen.insert(n).second)
        continue;
      if (dead.contains(nodes[n].raw))
        return false;
      cone.push_back(n);
      for (Value in : nodes[n].ins) {
        if (cutContains(cut, in))
          continue;
        auto it = nodeOf.find(in);
        if (it == nodeOf.end())
          return false; // a non-leaf primary input: cut is not well formed
        stack.push_back(it->second);
      }
    }
    return true;
  }

  /// Evaluate the cone's function over the cut leaves.
  bool computeCutFunction(unsigned root, const Cut &cut,
                          ArrayRef<unsigned> cone, ArrayRef<MNode> nodes,
                          const llvm::DenseMap<Value, unsigned> &nodeOf,
                          TruthTable &result) {
    // Elementary truth tables for the leaves, over |cut| variables.
    llvm::DenseMap<Value, TruthTable> val;
    unsigned nv = cut.size();
    for (unsigned j = 0; j < nv; ++j) {
      TruthTable t = 0;
      for (unsigned m = 0; m < (1u << nv); ++m)
        if ((m >> j) & 1)
          t |= (static_cast<TruthTable>(1) << m);
      val[cut[j]] = t;
    }
    // Cone is collected root-first; evaluate in reverse so fanins come
    // first (the graph was built in topological block order).
    for (unsigned idx = cone.size(); idx-- > 0;) {
      const MNode &n = nodes[cone[idx]];
      SmallVector<TruthTable, 6> ins;
      for (Value in : n.ins) {
        auto it = val.find(in);
        if (it == val.end())
          return false;
        ins.push_back(it->second);
      }
      TruthTable t = 0;
      for (unsigned m = 0; m < (1u << nv); ++m) {
        unsigned sel = 0;
        for (unsigned j = 0; j < ins.size(); ++j)
          sel |= (unsigned)((ins[j] >> m) & 1) << j;
        if ((n.cell->table >> sel) & 1)
          t |= (static_cast<TruthTable>(1) << m);
      }
      val[n.out] = t;
    }
    auto it = val.find(nodes[root].out);
    if (it == val.end())
      return false;
    // Mask to the used minterms.
    TruthTable mask = (nv >= 6) ? ~(TruthTable)0
                                : ((static_cast<TruthTable>(1) << (1u << nv)) - 1);
    result = it->second & mask;
    return true;
  }

  /// The nodes that actually become unused once the root's output is
  /// replaced, and their total area.
  ///
  /// This must be a FIXPOINT from the root, not "every use is inside the
  /// cone". A cone node X whose only user Y is also in the cone is NOT
  /// freed if Y itself survives (Y has a user outside). Crediting X's
  /// area in that case over-counts the saving and makes the pass grow the
  /// netlist while reporting a win -- measured: the area grew while the
  /// pass claimed a large saving.
  void computeMffc(unsigned root, ArrayRef<unsigned> cone,
                   ArrayRef<MNode> nodes,
                   const llvm::DenseMap<Value, unsigned> &nodeOf,
                   const llvm::DenseSet<Operation *> &dead,
                   SmallVector<unsigned> &mffc, double &area) {
    llvm::DenseSet<Operation *> freed;
    freed.insert(nodes[root].raw);
    bool changed = true;
    while (changed) {
      changed = false;
      for (unsigned n : cone) {
        if (freed.contains(nodes[n].raw))
          continue;
        bool allFreed = true;
        for (Operation *user : nodes[n].out.getUsers())
          if (!freed.contains(user)) {
            allFreed = false;
            break;
          }
        // A node with no users at all is already dead; leave it out.
        if (allFreed && !nodes[n].out.use_empty()) {
          freed.insert(nodes[n].raw);
          changed = true;
        }
      }
    }
    for (unsigned n : cone)
      if (freed.contains(nodes[n].raw)) {
        mffc.push_back(n);
        area += nodes[n].cell->area;
      }
  }

  hw::HWModuleExternOp lookupExtern(mlir::ModuleOp top,
                                    const CellPattern *cell, Type i1) {
    for (auto ext : top.getOps<hw::HWModuleExternOp>())
      if (ext.getModuleName() == cell->name)
        return ext;
    SmallVector<hw::PortInfo, 7> ports;
    for (auto [idx, pin] : llvm::enumerate(cell->inputPins)) {
      hw::PortInfo p;
      p.name = StringAttr::get(top.getContext(), pin);
      p.type = i1;
      p.dir = hw::ModulePort::Direction::Input;
      p.argNum = idx;
      ports.push_back(p);
    }
    hw::PortInfo o;
    o.name = StringAttr::get(top.getContext(), cell->outputPin);
    o.type = i1;
    o.dir = hw::ModulePort::Direction::Output;
    o.argNum = 0;
    ports.push_back(o);
    OpBuilder b = OpBuilder::atBlockBegin(top.getBody());
    return hw::HWModuleExternOp::create(
        b, top.getLoc(), StringAttr::get(top.getContext(), cell->name), ports);
  }
};

} // namespace

std::unique_ptr<mlir::OperationPass<mlir::ModuleOp>> createResubMappedPass() {
  return std::make_unique<ResubMappedPass>();
}

} // namespace circt_abc

//===- CombFacts.h - i1 facts implied by asserting comb values ---*- C++ -*-===//
//
// A small, sound implication engine over comb ops, shared by
// SpecializeMuxArms (rewrite a mux arm under its select) and
// ClockGateInference (rewrite a gated register's load under its enable).
//
// `add(v, val)` asserts that i1 `v` has value `val` and records what that
// implies, a few levels deep:
//   and(x..) = 1  =>  every x = 1          or(x..) = 0  =>  every x = 0
//   xor(x, 1) = b =>  x = !b
//   icmp eq/ceq v, K = 1  (or ne/cne = 0)  =>  v == K
//   icmp eq/ceq v, K = 0  (or ne/cne = 1)  =>  v != K
//   for i1 v: icmp eq/ceq v, 1 = b  =>  v = b  (and v, 0 => v = !b)
// `lookup(v)` answers an i1 the facts decide: a recorded bit, or an
// eq/ne compare against a constant whose operand has a recorded value.
// It never guesses: std::nullopt means "not decided", never "false".
//
//===----------------------------------------------------------------------===//

#ifndef CIRCT_ABC_STRUCTURALMAPPER_COMBFACTS_H
#define CIRCT_ABC_STRUCTURALMAPPER_COMBFACTS_H

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallVector.h"

#include <optional>

namespace circt_abc {

struct CombFacts {
  llvm::DenseMap<mlir::Value, bool> bits;
  // value -> the constant it equals / the constants it differs from
  llvm::DenseMap<mlir::Value, llvm::APInt> eq;
  llvm::DenseMap<mlir::Value, llvm::SmallVector<llvm::APInt, 2>> ne;

  static std::optional<llvm::APInt> constOf(mlir::Value v) {
    if (auto c = v.getDefiningOp<circt::hw::ConstantOp>())
      return c.getValue();
    return std::nullopt;
  }

  /// An eq/ne-style compare against a constant: (operand, constant, isEq).
  /// Case equality (ceq/cne) is treated as eq/ne: for synthesis there is no
  /// X, and both lower to the same gates.
  static std::optional<std::tuple<mlir::Value, llvm::APInt, bool>>
  constCompare(circt::comb::ICmpOp ic) {
    using P = circt::comb::ICmpPredicate;
    P p = ic.getPredicate();
    bool isEq = p == P::eq || p == P::ceq;
    bool isNe = p == P::ne || p == P::cne;
    if (!isEq && !isNe)
      return std::nullopt;
    mlir::Value lhs = ic.getLhs();
    auto k = constOf(ic.getRhs());
    if (!k) {
      k = constOf(ic.getLhs());
      lhs = ic.getRhs();
    }
    if (!k)
      return std::nullopt;
    return std::make_tuple(lhs, *k, isEq);
  }

  void add(mlir::Value v, bool val, unsigned depth = 0) {
    if (depth > 4)
      return;
    auto [it, inserted] = bits.try_emplace(v, val);
    if (!inserted)
      return;
    mlir::Operation *op = v.getDefiningOp();
    if (!op)
      return;
    if (auto a = llvm::dyn_cast<circt::comb::AndOp>(op); a && val)
      for (mlir::Value x : a.getInputs())
        add(x, true, depth + 1);
    if (auto o = llvm::dyn_cast<circt::comb::OrOp>(op); o && !val)
      for (mlir::Value x : o.getInputs())
        add(x, false, depth + 1);
    if (auto x = llvm::dyn_cast<circt::comb::XorOp>(op);
        x && x.getInputs().size() == 2) {
      for (unsigned k = 0; k < 2; ++k)
        if (auto c = constOf(x.getInputs()[k]); c && c->isAllOnes())
          add(x.getInputs()[1 - k], !val, depth + 1);
    }
    if (auto ic = llvm::dyn_cast<circt::comb::ICmpOp>(op)) {
      auto cmp = constCompare(ic);
      if (!cmp)
        return;
      auto [lhs, k, isEq] = *cmp;
      bool equal = isEq == val; // the compare says lhs == k
      if (k.getBitWidth() == 1) {
        add(lhs, equal ? k.isAllOnes() : !k.isAllOnes(), depth + 1);
        return;
      }
      if (equal)
        eq.try_emplace(lhs, k);
      else
        ne[lhs].push_back(k);
    }
  }

  /// Known value of an i1, if the facts decide it.
  std::optional<bool> lookup(mlir::Value v, unsigned depth = 0) const {
    if (depth > 8)
      return std::nullopt;
    if (auto it = bits.find(v); it != bits.end())
      return it->second;
    if (auto x = v.getDefiningOp<circt::comb::XorOp>();
        x && x.getInputs().size() == 2) {
      for (unsigned k = 0; k < 2; ++k)
        if (auto c = constOf(x.getInputs()[k]); c && c->isAllOnes())
          if (auto b = lookup(x.getInputs()[1 - k], depth + 1))
            return !*b;
    }
    auto ic = v.getDefiningOp<circt::comb::ICmpOp>();
    if (!ic)
      return std::nullopt;
    auto cmp = constCompare(ic);
    if (!cmp)
      return std::nullopt;
    auto [lhs, k, isEq] = *cmp;
    if (k.getBitWidth() == 1) {
      if (auto it = bits.find(lhs); it != bits.end()) {
        bool same = it->second == k.isAllOnes();
        return isEq ? same : !same;
      }
      return std::nullopt;
    }
    if (auto it = eq.find(lhs); it != eq.end()) {
      bool same = it->second == k;
      return isEq ? same : !same;
    }
    if (auto it = ne.find(lhs); it != ne.end())
      for (const llvm::APInt &x : it->second)
        if (x == k)
          return isEq ? false : true;
    return std::nullopt;
  }
};

} // namespace circt_abc

#endif // CIRCT_ABC_STRUCTURALMAPPER_COMBFACTS_H

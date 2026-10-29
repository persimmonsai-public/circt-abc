//===- IROrder.h - Deterministic IR-position order on Values ----*- C++ -*-===//
//
// A strict weak (in fact total) order on Values by where they are defined in
// the IR, for passes that sort Values to get a canonical form.
//
// Sorting by `Value::getAsOpaquePointer()` gives a canonical form that is
// stable WITHIN one process but not across processes: the pointers are heap
// addresses, so the order -- and every operand list built from it -- changes
// from run to run on identical input. Anything that only tests equality of
// the sorted lists is unaffected, but the moment the sorted order reaches the
// IR (operand order of a new comb.and, which cube of a DNF becomes which
// comb.or operand) the pass stops being a function of its input, and every
// downstream pass inherits the difference.
//
// `irOrderLess` orders by definition position instead: block arguments of a
// block before its operations, block arguments by number, operations by
// their position in the block, results of one operation by number. Values in
// different blocks are compared at the closest block that contains an
// ancestor of both. It is stateless and valid for values created during the
// pass (inserting an op never changes the relative order of existing ones),
// so a container sorted by it stays sorted while the pass builds IR -- as
// long as the pass does not MOVE ops that such a container already holds.
//
//===----------------------------------------------------------------------===//

#ifndef CIRCT_ABC_IRORDER_H
#define CIRCT_ABC_IRORDER_H

#include "mlir/IR/Block.h"
#include "mlir/IR/Operation.h"
#include "mlir/IR/Region.h"
#include "mlir/IR/Value.h"

#include <iterator>

namespace circt_abc {

namespace ir_order_detail {

// A program point: block argument `idx` of `block` (op == nullptr), or
// result `idx` of `op` (block == op->getBlock()).
struct Point {
  mlir::Block *block = nullptr;
  mlir::Operation *op = nullptr;
  unsigned idx = 0;
};

inline Point pointOf(mlir::Value v) {
  if (auto arg = mlir::dyn_cast<mlir::BlockArgument>(v))
    return {arg.getOwner(), nullptr, arg.getArgNumber()};
  auto res = mlir::cast<mlir::OpResult>(v);
  return {res.getOwner()->getBlock(), res.getOwner(), res.getResultNumber()};
}

// Order of two distinct blocks with the same parent op: region number, then
// position within the region.
inline bool siblingBlockLess(mlir::Block *a, mlir::Block *b) {
  mlir::Region *ra = a->getParent(), *rb = b->getParent();
  if (ra != rb)
    return ra->getRegionNumber() < rb->getRegionNumber();
  for (mlir::Block &blk : *ra) {
    if (&blk == a)
      return true;
    if (&blk == b)
      return false;
  }
  return false;
}

// -1 / 0 / +1 for two points in the SAME block.
inline int compareInBlock(const Point &a, const Point &b) {
  if (!a.op || !b.op) {
    if (a.op)
      return 1; // block arguments come first
    if (b.op)
      return -1;
    return a.idx < b.idx ? -1 : a.idx > b.idx ? 1 : 0;
  }
  if (a.op != b.op)
    return a.op->isBeforeInBlock(b.op) ? -1 : 1;
  return a.idx < b.idx ? -1 : a.idx > b.idx ? 1 : 0;
}

// Lift `p` to the ancestor point in block `target`, if `target` encloses it.
// A value nested in the regions of op X is placed at X itself; ties with X's
// own results are then broken by nesting (inner first), see compare().
inline bool liftTo(Point p, mlir::Block *target, Point &out, bool &nested) {
  nested = false;
  if (p.block == target) {
    out = p;
    return true;
  }
  mlir::Operation *parent = p.block ? p.block->getParentOp() : nullptr;
  if (!parent)
    return false;
  mlir::Operation *ancestor = target->findAncestorOpInBlock(*parent);
  if (!ancestor)
    return false;
  out = {target, ancestor, 0};
  nested = true;
  return true;
}

inline int compare(const Point &a, const Point &b) {
  if (a.block == b.block)
    return compareInBlock(a, b);
  Point la, lb;
  bool na, nb;
  // b's block encloses a?
  if (liftTo(a, b.block, la, na)) {
    if (b.op && la.op == b.op)
      return -1; // a is inside b's defining op: defined before b's results
    int c = compareInBlock(la, b);
    return c ? c : -1;
  }
  // a's block encloses b?
  if (liftTo(b, a.block, lb, nb)) {
    if (a.op && lb.op == a.op)
      return 1;
    int c = compareInBlock(a, lb);
    return c ? c : 1;
  }
  // Neither encloses the other: compare the blocks' parent ops, then (same
  // parent op) the blocks themselves.
  mlir::Operation *pa = a.block->getParentOp(), *pb = b.block->getParentOp();
  if (!pa || !pb)
    return 0; // detached IR: no meaningful order
  if (pa == pb)
    return siblingBlockLess(a.block, b.block) ? -1 : 1;
  return compare({pa->getBlock(), pa, 0}, {pb->getBlock(), pb, 0});
}

} // namespace ir_order_detail

/// True if `a` is defined before `b` in the IR. Deterministic across
/// processes, unlike comparing `getAsOpaquePointer()`.
inline bool irOrderLess(mlir::Value a, mlir::Value b) {
  if (a == b)
    return false;
  return ir_order_detail::compare(ir_order_detail::pointOf(a),
                                  ir_order_detail::pointOf(b)) < 0;
}

} // namespace circt_abc

#endif // CIRCT_ABC_IRORDER_H

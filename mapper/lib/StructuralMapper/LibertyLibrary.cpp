//===- LibertyLibrary.cpp -------------------------------------*- C++ -*-===//
//
// See LibertyLibrary.h for the "why". Two independent stages:
//   1. A brace-depth-tracking scanner pulls out cell name / area / pin
//      (name, direction, function) tuples, skipping test_cell()/pg_pin()
//      sub-blocks and any cell containing a ff()/latch() block.
//   2. A tiny recursive-descent parser turns each output pin's `function`
//      string into a boolean-expression AST over the cell's own input pin
//      indices, which is then brute-force-evaluated over all 2^arity
//      assignments to get a canonical truth table (canonical *for that
//      cell's own declared pin order* -- StructuralMapperPass.cpp tries all
//      permutations of a cone's leaves against this at match time).
//
//===----------------------------------------------------------------------===//

#include "LibertyLibrary.h"

#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/raw_ostream.h"
#include <algorithm>
#include <memory>

using namespace llvm;

namespace circt_abc {

namespace {

//===----------------------------------------------------------------------===//
// Stage 1: brace-depth-tracking cell/pin scanner.
//===----------------------------------------------------------------------===//

struct RawPin {
  std::string name;
  bool isOutput = false;
  bool sawDirection = false;
  bool isInternal = false; // `direction : internal ;` (a cell's own state)
  std::string function;
  // Always extracted (regardless of direction) since a liberty flip-flop
  // cell's clock/data pins are identified by these attributes, not by
  // direction alone -- see buildFFPatterns().
  bool isClock = false;   // `clock : true ;`
  bool isDataPin = false; // `nextstate_type : data ;`
  // `clock_gate_{clock,enable,test,out}_pin : true ;` on an integrated
  // clock gate's pins: "clock", "enable", "test" or "out"; empty otherwise.
  std::string clockGateRole;
};

struct RawCell {
  std::string name;
  double area = 0.0;
  bool hasSequential = false;
  bool hasFF = false;
  std::string ffBody; // interior text of the `ff(...) { ... }` block, if any
  // The two state-name identifiers from `ff(STATE,STATE_N) { ... }` -- an
  // output pin's `function` must equal `ffStateName` (not `ffStateNameN`)
  // to be a non-inverted Q pin; see buildFFPatterns()'s output-polarity
  // filter.
  std::string ffStateName;
  std::string ffStateNameN;
  std::vector<RawPin> pins;
  // Cell-level (not nested-block) attributes, for ICG cell extraction --
  // see buildICGPattern(). Both empty/empty-vector if absent, which is the
  // overwhelming common case (only a handful of cells in this PDK are
  // integrated clock-gating cells at all).
  std::string clockGatingType; // `clock_gating_integrated_cell : "..." ;`
  std::vector<std::string> pinNameMap; // `pin_name_map(A, B, ...);`
};

// Skips whitespace and returns the index of the next non-space character.
size_t skipSpace(StringRef s, size_t i) {
  while (i < s.size() && ::isspace((unsigned char)s[i]))
    ++i;
  return i;
}

// Finds the '{' that opens the block starting at/after `i`, and returns the
// index just past the matching '}'. `bodyBegin`/`bodyEnd` (if non-null) are
// set to the half-open range of the block body (exclusive of the braces).
// Returns StringRef::npos on unbalanced input.
size_t scanBracedBlock(StringRef s, size_t openBrace, size_t *bodyBegin,
                        size_t *bodyEnd) {
  assert(s[openBrace] == '{');
  size_t depth = 1;
  size_t i = openBrace + 1;
  if (bodyBegin)
    *bodyBegin = i;
  for (; i < s.size() && depth > 0; ++i) {
    if (s[i] == '{')
      ++depth;
    else if (s[i] == '}')
      --depth;
  }
  if (depth != 0)
    return StringRef::npos;
  if (bodyEnd)
    *bodyEnd = i - 1;
  return i;
}

// Extracts a `key : "value" ;` or `key : value ;` scalar attribute's value
// from a (non-nested) attribute-only region of text. Returns empty string
// if not found.
std::string extractScalarAttr(StringRef body, StringRef key) {
  size_t pos = 0;
  while (true) {
    pos = body.find(key, pos);
    if (pos == StringRef::npos)
      return {};
    // Require the match to be a whole identifier (not a suffix of a longer
    // key name) by checking the char before/after.
    bool beforeOk = pos == 0 || !::isalnum((unsigned char)body[pos - 1]);
    size_t afterIdx = pos + key.size();
    bool afterOk = afterIdx >= body.size() ||
                   (!::isalnum((unsigned char)body[afterIdx]) && body[afterIdx] != '_');
    if (!beforeOk || !afterOk) {
      pos += key.size();
      continue;
    }
    size_t i = skipSpace(body, pos + key.size());
    if (i >= body.size() || body[i] != ':')
      return {};
    i = skipSpace(body, i + 1);
    if (i < body.size() && body[i] == '"') {
      size_t end = body.find('"', i + 1);
      if (end == StringRef::npos)
        return {};
      return body.substr(i + 1, end - i - 1).str();
    }
    // Bare (unquoted) value: read until ';' or whitespace.
    size_t end = i;
    while (end < body.size() && body[end] != ';' && !::isspace((unsigned char)body[end]))
      ++end;
    return body.substr(i, end - i).str();
  }
}

// Scans `body` (the interior of a `cell(NAME) { ... }` block) for direct
// `pin(NAME) { ... }` children, skipping nested test_cell()/pg_pin()
// sub-blocks and ff()/latch() blocks (flagging `hasSequential` when seen)
// exactly like scripts/gen_liberty_blackbox_stubs.py's brace-depth approach.
void scanCellBody(StringRef body, RawCell &cell) {
  size_t i = 0;
  while (i < body.size()) {
    // Look for the next identifier immediately followed by '(' -- a
    // pin(...)/test_cell(...)/pg_pin(...)/ff(...)/latch(...) construct.
    size_t idStart = i;
    while (idStart < body.size() && !::isalpha((unsigned char)body[idStart]) &&
           body[idStart] != '_')
      ++idStart;
    if (idStart >= body.size())
      break;
    size_t idEnd = idStart;
    while (idEnd < body.size() &&
           (::isalnum((unsigned char)body[idEnd]) || body[idEnd] == '_'))
      ++idEnd;
    StringRef ident = body.slice(idStart, idEnd);
    size_t afterId = skipSpace(body, idEnd);

    if (afterId >= body.size() || body[afterId] != '(') {
      i = idEnd;
      continue;
    }
    size_t argsEnd = body.find(')', afterId);
    if (argsEnd == StringRef::npos) {
      i = idEnd;
      continue;
    }
    size_t braceStart = skipSpace(body, argsEnd + 1);
    if (braceStart >= body.size() || body[braceStart] != '{') {
      // e.g. `pin_name_map(A, B, ...);` with no body -- capture its
      // comma-separated identifier list (used by buildICGPattern()) before
      // skipping past the `)`.
      if (ident == "pin_name_map") {
        StringRef args = body.slice(afterId + 1, argsEnd);
        SmallVector<StringRef, 4> parts;
        args.split(parts, ',');
        for (StringRef part : parts)
          cell.pinNameMap.push_back(part.trim().str());
      }
      i = argsEnd + 1;
      continue;
    }

    size_t blockBodyBegin, blockBodyEnd;
    size_t next =
        scanBracedBlock(body, braceStart, &blockBodyBegin, &blockBodyEnd);
    if (next == StringRef::npos) {
      i = braceStart + 1;
      continue;
    }
    StringRef blockBody = body.slice(blockBodyBegin, blockBodyEnd);

    if (ident == "pin") {
      StringRef pinName = body.slice(afterId + 1, argsEnd).trim();
      // A liberty `pin(A, B)` group declares several pins with shared
      // attributes -- not used for combinational-cell function pins in
      // this PDK's cells; treat as a single opaque pin name if it occurs
      // (won't match any function identifier, so it's harmless if wrong).
      RawPin pin;
      pin.name = pinName.str();
      std::string dir = extractScalarAttr(blockBody, "direction");
      pin.sawDirection = !dir.empty();
      pin.isOutput = (dir == "output");
      pin.isInternal = (dir == "internal");
      if (pin.isOutput)
        pin.function = extractScalarAttr(blockBody, "function");
      pin.isClock = extractScalarAttr(blockBody, "clock") == "true";
      pin.isDataPin = extractScalarAttr(blockBody, "nextstate_type") == "data";
      for (StringRef role : {"clock", "enable", "test", "out"})
        if (extractScalarAttr(blockBody,
                              ("clock_gate_" + role + "_pin").str()) == "true")
          pin.clockGateRole = role.str();
      cell.pins.push_back(std::move(pin));
    } else if (ident == "ff") {
      cell.hasSequential = true;
      cell.hasFF = true;
      cell.ffBody = blockBody.str();
      // `ff(STATE,STATE_N)` -- split the two args on the comma so
      // buildFFPatterns() can tell a non-inverted Q pin (function ==
      // STATE) from an inverted one (function == STATE_N) without
      // hard-coding "IQ"/"IQN".
      StringRef ffArgs = body.slice(afterId + 1, argsEnd).trim();
      auto [stateName, stateNameN] = ffArgs.split(',');
      cell.ffStateName = stateName.trim().str();
      cell.ffStateNameN = stateNameN.trim().str();
    } else if (ident == "latch") {
      cell.hasSequential = true;
    }
    // test_cell(...)/pg_pin(...)/other sub-blocks: contents intentionally
    // NOT recursed into (their nested pin(...) groups redeclare functional
    // pins for DFT/power views and would double-count/corrupt otherwise).

    i = next;
  }
}

std::vector<RawCell> scanLibertyCells(StringRef text) {
  std::vector<RawCell> cells;
  size_t i = 0;
  while (true) {
    size_t idStart = text.find("cell", i);
    if (idStart == StringRef::npos)
      break;
    // Require whole-word match.
    bool beforeOk = idStart == 0 || !::isalnum((unsigned char)text[idStart - 1]);
    size_t afterIdent = idStart + 4;
    if (!beforeOk) {
      i = idStart + 4;
      continue;
    }
    size_t afterSpace = skipSpace(text, afterIdent);
    if (afterSpace >= text.size() || text[afterSpace] != '(') {
      i = afterIdent;
      continue;
    }
    size_t nameEnd = text.find(')', afterSpace);
    if (nameEnd == StringRef::npos)
      break;
    StringRef cellName = text.slice(afterSpace + 1, nameEnd).trim();
    size_t braceStart = skipSpace(text, nameEnd + 1);
    if (braceStart >= text.size() || text[braceStart] != '{') {
      i = nameEnd + 1;
      continue;
    }
    size_t bodyBegin, bodyEnd;
    size_t next = scanBracedBlock(text, braceStart, &bodyBegin, &bodyEnd);
    if (next == StringRef::npos)
      break;

    RawCell cell;
    cell.name = cellName.str();
    StringRef body = text.slice(bodyBegin, bodyEnd);
    std::string areaStr = extractScalarAttr(body, "area");
    if (!areaStr.empty())
      cell.area = std::strtod(areaStr.c_str(), nullptr);
    cell.clockGatingType = extractScalarAttr(body, "clock_gating_integrated_cell");
    scanCellBody(body, cell);
    cells.push_back(std::move(cell));

    i = next;
  }
  return cells;
}

//===----------------------------------------------------------------------===//
// Stage 2: boolean-expression parser + evaluator.
//===----------------------------------------------------------------------===//

struct FnNode {
  enum Kind { Var, Not, And, Xor, Or } kind;
  int varIndex = -1;
  std::unique_ptr<FnNode> a, b;
};

class FunctionParser {
public:
  FunctionParser(StringRef text, const std::vector<std::string> &pinNames)
      : text(text), pinNames(pinNames) {}

  std::unique_ptr<FnNode> parse() {
    skipSpace();
    auto node = parseOr();
    skipSpace();
    if (!node || pos != text.size())
      return nullptr;
    return node;
  }

private:
  StringRef text;
  const std::vector<std::string> &pinNames;
  size_t pos = 0;
  bool failed = false;

  void skipSpace() {
    while (pos < text.size() && ::isspace((unsigned char)text[pos]))
      ++pos;
  }
  char peek() { return pos < text.size() ? text[pos] : '\0'; }

  std::unique_ptr<FnNode> parseOr() {
    auto lhs = parseXor();
    if (!lhs)
      return nullptr;
    skipSpace();
    while (peek() == '|') {
      ++pos;
      skipSpace();
      auto rhs = parseXor();
      if (!rhs)
        return nullptr;
      auto node = std::make_unique<FnNode>();
      node->kind = FnNode::Or;
      node->a = std::move(lhs);
      node->b = std::move(rhs);
      lhs = std::move(node);
      skipSpace();
    }
    return lhs;
  }

  std::unique_ptr<FnNode> parseXor() {
    auto lhs = parseAnd();
    if (!lhs)
      return nullptr;
    skipSpace();
    while (peek() == '^') {
      ++pos;
      skipSpace();
      auto rhs = parseAnd();
      if (!rhs)
        return nullptr;
      auto node = std::make_unique<FnNode>();
      node->kind = FnNode::Xor;
      node->a = std::move(lhs);
      node->b = std::move(rhs);
      lhs = std::move(node);
      skipSpace();
    }
    return lhs;
  }

  std::unique_ptr<FnNode> parseAnd() {
    auto lhs = parseUnary();
    if (!lhs)
      return nullptr;
    skipSpace();
    while (peek() == '&') {
      ++pos;
      skipSpace();
      auto rhs = parseUnary();
      if (!rhs)
        return nullptr;
      auto node = std::make_unique<FnNode>();
      node->kind = FnNode::And;
      node->a = std::move(lhs);
      node->b = std::move(rhs);
      lhs = std::move(node);
      skipSpace();
    }
    return lhs;
  }

  std::unique_ptr<FnNode> parseUnary() {
    skipSpace();
    if (peek() == '!') {
      ++pos;
      skipSpace();
      auto operand = parseUnary();
      if (!operand)
        return nullptr;
      auto node = std::make_unique<FnNode>();
      node->kind = FnNode::Not;
      node->a = std::move(operand);
      return node;
    }
    return parsePrimary();
  }

  std::unique_ptr<FnNode> parsePrimary() {
    skipSpace();
    if (peek() == '(') {
      ++pos;
      auto inner = parseOr();
      if (!inner)
        return nullptr;
      skipSpace();
      if (peek() != ')')
        return nullptr;
      ++pos;
      return inner;
    }
    size_t start = pos;
    while (pos < text.size() &&
           (::isalnum((unsigned char)text[pos]) || text[pos] == '_'))
      ++pos;
    if (pos == start)
      return nullptr; // no identifier here -- parse error
    StringRef ident = text.slice(start, pos);
    auto it = std::find(pinNames.begin(), pinNames.end(), ident.str());
    if (it == pinNames.end())
      return nullptr; // references a pin we don't know about
    auto node = std::make_unique<FnNode>();
    node->kind = FnNode::Var;
    node->varIndex = std::distance(pinNames.begin(), it);
    return node;
  }
};

bool evalFn(const FnNode *n, unsigned assignment) {
  switch (n->kind) {
  case FnNode::Var:
    return (assignment >> n->varIndex) & 1;
  case FnNode::Not:
    return !evalFn(n->a.get(), assignment);
  case FnNode::And:
    return evalFn(n->a.get(), assignment) && evalFn(n->b.get(), assignment);
  case FnNode::Xor:
    return evalFn(n->a.get(), assignment) != evalFn(n->b.get(), assignment);
  case FnNode::Or:
    return evalFn(n->a.get(), assignment) || evalFn(n->b.get(), assignment);
  }
  llvm_unreachable("unhandled FnNode::Kind");
}

// Builds a CellPattern from a RawCell, or returns std::nullopt if the cell
// doesn't qualify (sequential, wrong pin shape, or unparseable function).
std::optional<CellPattern> buildPattern(const RawCell &raw) {
  if (raw.hasSequential)
    return std::nullopt;

  CellPattern pat;
  pat.name = raw.name;
  pat.area = raw.area;

  const RawPin *outputPin = nullptr;
  for (const RawPin &pin : raw.pins) {
    if (!pin.sawDirection)
      continue;
    if (pin.isOutput) {
      if (outputPin)
        return std::nullopt; // more than one output pin: not v1 scope
      outputPin = &pin;
    } else {
      pat.inputPins.push_back(pin.name);
    }
  }
  if (!outputPin || outputPin->function.empty())
    return std::nullopt;
  if (pat.inputPins.empty() || pat.inputPins.size() > 6)
    return std::nullopt;
  pat.outputPin = outputPin->name;

  std::vector<std::string> pinNames(pat.inputPins.begin(),
                                     pat.inputPins.end());
  FunctionParser parser(outputPin->function, pinNames);
  auto ast = parser.parse();
  if (!ast)
    return std::nullopt;

  unsigned arity = pat.inputPins.size();
  TruthTable table = 0;
  for (unsigned a = 0; a < (1u << arity); ++a)
    if (evalFn(ast.get(), a))
      // `TruthTable(1) << a`, NOT `1u << a` then cast: for arity up to 6,
      // `a` can be up to 63, and shifting a 32-bit `1u` by >=32 is
      // undefined behavior / silently wraps to 0 -- the cast to
      // TruthTable (uint64_t) must happen BEFORE the shift.
      table |= (static_cast<TruthTable>(1) << a);
  pat.table = table;
  return pat;
}

// Builds a HalfAdderCellPattern from a RawCell, or std::nullopt if the
// cell is not a two-input/two-output half adder. Deliberately narrow: the
// pass that consumes this only ever substitutes an exact (a^b, a&b) pair,
// so anything else is left for buildPattern() (which rejects it anyway,
// having a one-output-pin cap) rather than guessed at.
std::optional<HalfAdderCellPattern> buildHalfAdderPattern(const RawCell &raw) {
  if (raw.hasSequential || raw.hasFF)
    return std::nullopt;

  llvm::SmallVector<const RawPin *, 2> ins, outs;
  for (const RawPin &pin : raw.pins) {
    if (!pin.sawDirection)
      continue;
    if (pin.isOutput)
      outs.push_back(&pin);
    else
      ins.push_back(&pin);
  }
  if (ins.size() != 2 || outs.size() != 2)
    return std::nullopt;

  std::vector<std::string> pinNames{ins[0]->name, ins[1]->name};

  // Truth tables over (bit0 = ins[0], bit1 = ins[1]):
  //   a^b -> true at assignments 1 and 2 -> 0b0110 == 6
  //   a&b -> true at assignment 3 only   -> 0b1000 == 8
  const TruthTable kSum = 6, kCarry = 8;
  const RawPin *sum = nullptr, *carry = nullptr;
  for (const RawPin *out : outs) {
    if (out->function.empty())
      return std::nullopt;
    FunctionParser parser(out->function, pinNames);
    auto ast = parser.parse();
    if (!ast)
      return std::nullopt;
    TruthTable table = 0;
    for (unsigned a = 0; a < 4; ++a)
      if (evalFn(ast.get(), a))
        table |= (static_cast<TruthTable>(1) << a);
    if (table == kSum && !sum)
      sum = out;
    else if (table == kCarry && !carry)
      carry = out;
    else
      return std::nullopt; // not a half adder, or a duplicated output role
  }
  if (!sum || !carry)
    return std::nullopt;

  HalfAdderCellPattern pat;
  pat.name = raw.name;
  pat.area = raw.area;
  pat.inputPins.push_back(ins[0]->name);
  pat.inputPins.push_back(ins[1]->name);
  pat.sumPin = sum->name;
  pat.carryPin = carry->name;
  return pat;
}

// Builds a FullAdderCellPattern from a RawCell, or std::nullopt if the cell
// is not a 3-input/2-output full adder.  Same shape as the half-adder
// builder: evaluate both output functions over all 8 input assignments and
// require exactly the sum and majority tables.
std::optional<FullAdderCellPattern> buildFullAdderPattern(const RawCell &raw) {
  if (raw.hasSequential || raw.hasFF)
    return std::nullopt;

  llvm::SmallVector<const RawPin *, 3> ins, outs;
  for (const RawPin &pin : raw.pins) {
    if (!pin.sawDirection)
      continue;
    if (pin.isOutput)
      outs.push_back(&pin);
    else
      ins.push_back(&pin);
  }
  if (ins.size() != 3 || outs.size() != 2)
    return std::nullopt;

  std::vector<std::string> pinNames{ins[0]->name, ins[1]->name, ins[2]->name};

  // Truth tables over (bit0 = ins[0], bit1 = ins[1], bit2 = ins[2]):
  //   a^b^ci        -> true where the popcount is odd  -> {1,2,4,7} -> 0x96
  //   majority(...) -> true where the popcount is >= 2 -> {3,5,6,7} -> 0xE8
  const TruthTable kSum = 0x96, kCarry = 0xE8;
  const RawPin *sum = nullptr, *carry = nullptr;
  for (const RawPin *out : outs) {
    if (out->function.empty())
      return std::nullopt;
    FunctionParser parser(out->function, pinNames);
    auto ast = parser.parse();
    if (!ast)
      return std::nullopt;
    TruthTable table = 0;
    for (unsigned a = 0; a < 8; ++a)
      if (evalFn(ast.get(), a))
        table |= (static_cast<TruthTable>(1) << a);
    if (table == kSum && !sum)
      sum = out;
    else if (table == kCarry && !carry)
      carry = out;
    else
      return std::nullopt; // not a full adder, or a duplicated output role
  }
  if (!sum || !carry)
    return std::nullopt;

  FullAdderCellPattern pat;
  pat.name = raw.name;
  pat.area = raw.area;
  for (const RawPin *in : ins)
    pat.inputPins.push_back(in->name);
  pat.sumPin = sum->name;
  pat.carryPin = carry->name;
  return pat;
}

//===----------------------------------------------------------------------===//
// Stage 3: flip-flop (ff()-group) cell extraction.
//===----------------------------------------------------------------------===//

// A liberty expression that is a single literal, `X` or `!X`: returns X and
// sets `negated`, or returns "" for anything more complex.
StringRef literalOf(StringRef expr, bool &negated) {
  expr = expr.trim();
  while (expr.consume_front("(") && expr.consume_back(")"))
    expr = expr.trim();
  negated = expr.consume_front("!");
  expr = expr.trim();
  if (expr.empty() ||
      !llvm::all_of(expr, [](char c) { return ::isalnum(c) || c == '_'; }))
    return {};
  return expr;
}

// Builds the FFCellPatterns a RawCell whose ff() group was captured can
// serve as -- none if it doesn't match a supported shape: one clock pin
// (rising edge), one data pin, one output pin reading the state or its
// complement, and no other inputs beyond the clear/preset pins. A cell with
// both clear and preset yields two patterns, one per pin, the other pin
// tied inactive.
llvm::SmallVector<FFCellPattern, 2> buildFFPatterns(const RawCell &raw) {
  if (!raw.hasFF)
    return {};

  const RawPin *clockPin = nullptr;
  const RawPin *dataPin = nullptr;
  const RawPin *outputPin = nullptr;
  llvm::SmallVector<const RawPin *, 2> otherInputs;
  for (const RawPin &pin : raw.pins) {
    if (pin.isOutput) {
      if (outputPin)
        return {}; // more than one output pin: not our shape
      outputPin = &pin;
    } else if (pin.isClock) {
      if (clockPin)
        return {};
      clockPin = &pin;
    } else if (pin.isDataPin) {
      if (dataPin)
        return {};
      dataPin = &pin;
    } else {
      otherInputs.push_back(&pin);
    }
  }

  // The data pin and its polarity come from `next_state`. A library that
  // does not mark the data pin `nextstate_type : data` (ASAP7) is still
  // unambiguous when `next_state` is a single literal over one input pin.
  bool nextStateNegated = false;
  StringRef nextStateVar =
      literalOf(extractScalarAttr(raw.ffBody, "next_state"), nextStateNegated);
  if (!dataPin && !nextStateVar.empty()) {
    for (auto *it = otherInputs.begin(); it != otherInputs.end(); ++it)
      if ((*it)->name == nextStateVar) {
        dataPin = *it;
        otherInputs.erase(it);
        break;
      }
  }
  if (!clockPin || !dataPin || !outputPin)
    return {};
  // A marked data pin with a `next_state` we cannot read keeps the old
  // reading, D straight through.
  if (nextStateVar.empty() || nextStateVar != dataPin->name)
    nextStateNegated = false;

  // Output polarity: `ff(STATE,STATE_N)` names the internal state and its
  // complement; a pin whose `function` is STATE_N (or `!STATE`) outputs the
  // complement of the stored value. Together with `next_state`'s own
  // polarity this decides whether the output follows D or its complement.
  bool outputNegated;
  if (outputPin->function == raw.ffStateName)
    outputNegated = false;
  else if (outputPin->function == raw.ffStateNameN ||
           StringRef(outputPin->function).trim() == "!" + raw.ffStateName)
    outputNegated = true;
  else
    return {};

  // Clock-polarity filter: seq.firreg is unconditionally rising-edge on its
  // own `clk` (no polarity option on the op at all), so only a cell whose
  // `clocked_on` expression is the BARE clock pin name (no leading `!`) can
  // be wired directly. A `clocked_on : "!CKN"` cell (negative-edge-triggered)
  // would need an explicit clock inverter this mapper doesn't insert --
  // reject it rather than silently mapping to the wrong clock edge.
  std::string clockedOnExpr = extractScalarAttr(raw.ffBody, "clocked_on");
  if (StringRef(clockedOnExpr).trim().starts_with("!"))
    return {};

  FFCellPattern base;
  base.name = raw.name;
  base.area = raw.area;
  base.clockPin = clockPin->name;
  base.dataPin = dataPin->name;
  base.outputPin = outputPin->name;
  base.dataInverted = nextStateNegated != outputNegated;

  std::string clearExpr = extractScalarAttr(raw.ffBody, "clear");
  std::string presetExpr = extractScalarAttr(raw.ffBody, "preset");
  if (clearExpr.empty() && presetExpr.empty()) {
    if (!otherInputs.empty())
      return {}; // an unexplained extra pin: not the plain shape
    return {base};
  }

  // Each async pin must be a single literal over one of the remaining
  // inputs, and those must be all of them: a scan-testable variant (a scan
  // flop with SE/SI pins) would otherwise pass with its extra pins never
  // wired in the generated declaration.
  struct Async {
    StringRef pin;
    bool activeLow;
    bool toOne; // the output after this pin asserts
  };
  llvm::SmallVector<Async, 2> asyncPins;
  for (auto [expr, setsState] : {std::pair<StringRef, bool>{clearExpr, false},
                                 std::pair<StringRef, bool>{presetExpr, true}}) {
    if (expr.empty())
      continue;
    bool neg;
    StringRef pin = literalOf(expr, neg);
    if (pin.empty())
      return {};
    asyncPins.push_back({pin, neg, setsState != outputNegated});
  }
  if (otherInputs.size() != asyncPins.size())
    return {};
  for (const Async &ap : asyncPins)
    if (llvm::none_of(otherInputs,
                      [&](const RawPin *p) { return p->name == ap.pin; }))
      return {};

  llvm::SmallVector<FFCellPattern, 2> out;
  for (const Async &ap : asyncPins) {
    FFCellPattern pat = base;
    pat.hasReset = true;
    pat.resetPin = ap.pin.str();
    pat.resetActiveHigh = !ap.activeLow;
    pat.isSet = ap.toOne;
    for (const Async &other : asyncPins)
      if (other.pin != ap.pin)
        pat.tiedPins.push_back({other.pin.str(), other.activeLow});
    out.push_back(std::move(pat));
  }
  // The no-reset shape too, every async pin at its inactive level: for a
  // library whose only flops have async pins (GT3's one flop has both).
  FFCellPattern none = base;
  for (const Async &ap : asyncPins)
    none.tiedPins.push_back({ap.pin.str(), ap.activeLow});
  out.push_back(std::move(none));
  return out;
}

//===----------------------------------------------------------------------===//
// Stage 4: integrated clock-gating cell extraction.
//===----------------------------------------------------------------------===//

// Builds an ICGCellPattern from a RawCell, or returns std::nullopt if it
// isn't a validated posedge-precontrol integrated clock-gating cell with
// the expected 4-pin shape -- see ICGCellPattern's own comment for why
// `"latch_negedge_precontrol"` (wrong clock edge for this flow's target FF
// cells) and `"generic"` (unvalidated semantics) are excluded rather than
// guessed at, and why an unexpected `pin_name_map` shape is rejected
// outright rather than reordered.
std::optional<ICGCellPattern> buildICGPattern(const RawCell &raw) {
  if (raw.clockGatingType != "latch_posedge_precontrol")
    return std::nullopt;
  // No pin_name_map (ASAP7): the pins carry their roles themselves. All
  // four must be present exactly once, and nothing else may be an input.
  if (raw.pinNameMap.empty()) {
    ICGCellPattern pat;
    pat.name = raw.name;
    pat.area = raw.area;
    llvm::StringMap<std::string *> slot{{"clock", &pat.clockPin},
                                        {"enable", &pat.enablePin},
                                        {"test", &pat.scanEnablePin},
                                        {"out", &pat.outputPin}};
    for (const RawPin &pin : raw.pins) {
      if (!pin.sawDirection || pin.isInternal)
        continue; // the latch's own state, not a port
      auto it = slot.find(pin.clockGateRole);
      if (it == slot.end() || !it->second->empty())
        return std::nullopt;
      *it->second = pin.name;
    }
    for (auto &kv : slot)
      if (kv.second->empty())
        return std::nullopt;
    return pat;
  }
  if (raw.pinNameMap.size() != 4)
    return std::nullopt;
  if (raw.pinNameMap[0] != "CK" || raw.pinNameMap[1] != "E" ||
      raw.pinNameMap[2] != "SE" || raw.pinNameMap[3] != "ECK")
    return std::nullopt;

  ICGCellPattern pat;
  pat.name = raw.name;
  pat.area = raw.area;
  pat.clockPin = "CK";
  pat.enablePin = "E";
  pat.scanEnablePin = "SE";
  pat.outputPin = "ECK";
  return pat;
}

} // namespace

void LibertyLibrary::indexCell(const CellPattern &cell) {
  auto key = std::make_pair(cell.arity(), cell.table);
  auto it = exactIndex.find(key);
  if (it == exactIndex.end() || cell.area < it->second->area)
    exactIndex[key] = &cell;
}

void LibertyLibrary::indexFFCell(const FFCellPattern &cell) {
  const FFCellPattern **slot;
  if (!cell.hasReset)
    slot = &cheapestNoResetFF;
  else if (cell.isSet)
    slot = &cheapestSetFF;
  else
    slot = &cheapestClearFF;
  // Cost is the cell's area plus, for an inverted-data cell, an inverter;
  // at equal cost the cell needing neither an inverter nor a tied pin wins.
  auto key = [&](const FFCellPattern *c) {
    return std::make_pair(c->area + (c->dataInverted ? inverterArea : 0.0),
                          c->isFallback());
  };
  if (!*slot || key(&cell) < key(*slot))
    *slot = &cell;
}

void LibertyLibrary::indexICGCell(const ICGCellPattern &cell) {
  if (!cheapestICG || cell.area < cheapestICG->area)
    cheapestICG = &cell;
}

std::optional<LibertyLibrary> LibertyLibrary::loadFromFile(StringRef path) {
  auto bufferOrErr = llvm::MemoryBuffer::getFile(path);
  if (!bufferOrErr) {
    llvm::errs() << "LibertyLibrary: could not read " << path << "\n";
    return std::nullopt;
  }

  LibertyLibrary lib;
  std::vector<RawCell> rawCells = scanLibertyCells((*bufferOrErr)->getBuffer());
  for (const RawCell &raw : rawCells) {
    if (auto pat = buildPattern(raw))
      lib.cells.push_back(std::move(*pat));
    for (FFCellPattern &ffPat : buildFFPatterns(raw))
      lib.ffCells.push_back(std::move(ffPat));
    if (auto icgPat = buildICGPattern(raw))
      lib.icgCells.push_back(std::move(*icgPat));
    if (auto haPat = buildHalfAdderPattern(raw))
      lib.halfAdderCells.push_back(std::move(*haPat));
    if (auto faPat = buildFullAdderPattern(raw))
      lib.fullAdderCells.push_back(std::move(*faPat));
  }

  // Build the indices only after `cells`/`ffCells`/`icgCells` are done
  // growing, so the pointers `indexCell`/`indexFFCell`/`indexICGCell` store
  // stay valid for the library's lifetime.
  for (const CellPattern &cell : lib.cells)
    lib.indexCell(cell);
  if (auto inv = lib.exactIndex.find({1, TruthTable(0b01)});
      inv != lib.exactIndex.end())
    lib.inverterArea = inv->second->area;
  for (const FFCellPattern &cell : lib.ffCells)
    lib.indexFFCell(cell);
  for (const ICGCellPattern &cell : lib.icgCells)
    lib.indexICGCell(cell);
  for (const HalfAdderCellPattern &cell : lib.halfAdderCells)
    if (!lib.cheapestHalfAdder || cell.area < lib.cheapestHalfAdder->area)
      lib.cheapestHalfAdder = &cell;
  for (const FullAdderCellPattern &cell : lib.fullAdderCells)
    if (!lib.cheapestFullAdder || cell.area < lib.cheapestFullAdder->area)
      lib.cheapestFullAdder = &cell;

  return lib;
}

static TruthTable permuteInputs(TruthTable table, unsigned arity,
                                 ArrayRef<unsigned> cellSlotToLeaf) {
  TruthTable result = 0;
  for (unsigned assign = 0; assign < (1u << arity); ++assign) {
    unsigned leafAssign = 0;
    for (unsigned slot = 0; slot < arity; ++slot) {
      unsigned bit = (assign >> slot) & 1;
      leafAssign |= bit << cellSlotToLeaf[slot];
    }
    unsigned outBit = (table >> leafAssign) & 1;
    // Cast before shifting, same reasoning as buildPattern(): `assign` can
    // be up to 63 for arity 6, and `outBit` is a 32-bit `unsigned`.
    result |= (static_cast<TruthTable>(outBit) << assign);
  }
  return result;
}

std::optional<CellMatch> LibertyLibrary::lookup(unsigned arity,
                                                 TruthTable table,
                                                 bool inversionIsFree) const {
  if (arity == 0 || arity > 6)
    return std::nullopt;

  unsigned numRows = 1u << arity; // <= 64, safe as a 32-bit shift/value.
  // A full 64-row table (arity == 6) occupies every bit of TruthTable --
  // `(TruthTable(1) << 64) - 1` would itself be undefined behavior (shift
  // amount equal to the type's bit width), so that case is all-ones
  // directly rather than computed via shift.
  TruthTable mask = (numRows >= 64) ? ~TruthTable(0)
                                     : ((static_cast<TruthTable>(1) << numRows) - 1);

  SmallVector<unsigned, 6> perm;
  for (unsigned i = 0; i < arity; ++i)
    perm.push_back(i);

  // An inverted match costs the cell PLUS an inverter and a second level of
  // logic; price the inverter (cheapest arity-1 NOT cell) into the
  // comparison so a direct AND2/OR2 beats NAND2/NOR2 + INV. Before this the
  // cheapest-cell rule alone chose NOR2 + INV for every shell OR (cv64a6:
  // 3,535 NOR2 + 3,724 INV shell cells, eight such pairs on the worst
  // register-to-register path).
  double invArea = 0.0;
  {
    auto inv = exactIndex.find(std::make_pair(1u, static_cast<TruthTable>(0b01)));
    if (inv != exactIndex.end())
      invArea = inv->second->area;
  }
  std::optional<CellMatch> best;
  double bestCost = 0.0;
  auto consider = [&](TruthTable queryTable, bool inverted) {
    TruthTable permuted = permuteInputs(queryTable, arity, perm);
    auto it = exactIndex.find(std::make_pair(arity, permuted));
    if (it == exactIndex.end())
      return;
    double cost = it->second->area +
                  (inverted && !inversionIsFree ? invArea : 0.0);
    if (!best || cost < bestCost ||
        (cost == bestCost && !inverted && best->inverted)) {
      bestCost = cost;
      CellMatch match;
      match.cell = it->second;
      match.leafForCellInputSlot.assign(perm.begin(), perm.end());
      match.inverted = inverted;
      best = std::move(match);
    }
  };

  do {
    // Try the query as-is, then its complement -- most of this PDK's real
    // compound gates (AOI/OAI-class) are inherently output-inverted, so
    // skipping the complement here would make them essentially unreachable
    // from a plain comb.mux/and/or/xor cone (see CellMatch::inverted).
    consider(table, /*inverted=*/false);
    consider(static_cast<TruthTable>(~table & mask), /*inverted=*/true);
  } while (std::next_permutation(perm.begin(), perm.end()));

  return best;
}

} // namespace circt_abc

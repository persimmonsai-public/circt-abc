//===- LibertyLibrary.h - liberty-derived cell pattern library -*- C++ -*-===//
//
// M2: builds an {arity, truth-table} -> cheapest-matching-cell index from a
// real liberty file (the same file ABC/Yosys map against, for comparability).
// This is intentionally NOT a generic liberty parser: it understands the common
// subset liberty files use for combinational cell `function` strings
// (identifiers, `!` prefix-not, `&`/`|`/`^` infix, parens), and
// brace-depth-tracks cell/pin blocks the same way
// scripts/gen_liberty_blackbox_stubs.py's Python parser does (skipping
// test_cell()/pg_pin() sub-blocks that redeclare pins for DFT/power views).
//
//===----------------------------------------------------------------------===//

#ifndef CIRCT_ABC_LIBERTY_LIBRARY_H
#define CIRCT_ABC_LIBERTY_LIBRARY_H

#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringRef.h"
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace circt_abc {

// A truth table over <=6 boolean inputs: bit `a` (0..63) is the cell's
// output for the input assignment where input pin `i` (in `inputPins`
// order) gets bit `i` of `a`. Widened from an original <=4-input cap
// (uint16_t) to 6 inputs (uint64_t) for wide-select cells like a MUX4
// (4 data pins + 2 select pins) -- see StructuralMapperPass.cpp's
// "Pattern 2: nested mux-of-mux fusion".
using TruthTable = uint64_t;

struct CellPattern {
  std::string name;
  double area = 0.0;
  llvm::SmallVector<std::string, 4> inputPins;
  std::string outputPin;
  TruthTable table = 0;

  unsigned arity() const { return inputPins.size(); }
};

struct CellMatch {
  const CellPattern *cell;
  // leafForCellInputSlot[slot] = index into the caller's leaf list that
  // should be wired to the cell's inputPins[slot].
  llvm::SmallVector<unsigned, 6> leafForCellInputSlot;
  // True if the cell computes the COMPLEMENT of the query's truth table --
  // most of this PDK's actual compound-gate cells (AOI21/AOI22/OAI21/...)
  // are inherently output-inverted (`function : "(!(...))"`, cheaper in
  // CMOS than an equivalent non-inverting gate), so a query built from
  // plain comb.mux/and/or/xor almost never matches one directly without
  // also trying its complement. When true, the caller must invert the
  // instance's output (a single extra INV, or -- better -- push the
  // inversion into the consumer) to get the query's actual value.
  bool inverted = false;
};

// A flip-flop cell extracted from a liberty `ff(...)` group. Unlike
// CellPattern (combinational cells, matched via brute-force truth table +
// permutation search), FF-cell "matching" is trivial: there are only 3
// possible shapes given this op set (no reset / async clear / async
// preset), and pin identity comes straight from the liberty text (`clock:
// true`, `nextstate_type: data`, and the polarity of the `clear`/`preset`
// expression) rather than from any function/truth-table analysis -- an
// ff() group's `next_state`/`clocked_on` describe sequential (clocked)
// behavior, which is not a boolean function of its input pins the way a
// combinational cell's `function` string is.
struct FFCellPattern {
  std::string name;
  double area = 0.0;
  std::string clockPin;
  std::string dataPin;
  std::string outputPin;
  bool hasReset = false;
  std::string resetPin;     // valid iff hasReset
  bool resetActiveHigh = true; // valid iff hasReset: polarity that TRIGGERS
                                // the clear/preset (true = bare pin name in
                                // the liberty expression, false = "!pin").
  bool isSet = false;        // valid iff hasReset: true = preset-class
                              // (resets/sets data to 1), false = clear-class
                              // (resets to 0). Measured at the output pin,
                              // so an inverted-output cell's `clear` makes
                              // it preset-class.
  // The output pin carries the complement of D (an odd number of the
  // `next_state : "!D"` and output-reads-STATE_N inversions): the mapper
  // drives D with the complement of the register's next value.
  bool dataInverted = false;
  // Async pins the shape does not use, each held at its inactive level: a
  // cell with both clear and preset used as a single-reset cell ties the
  // other one, and used as a no-reset cell ties both.
  llvm::SmallVector<std::pair<std::string, bool>, 2> tiedPins;

  // Shapes that need an inverter or a tied pin; at equal cost a cell that
  // needs neither wins.
  bool isFallback() const { return dataInverted || !tiedPins.empty(); }
};

// An integrated clock-gating cell extracted from the liberty file, for
// ClockGateInferencePass. Unlike CellPattern/FFCellPattern, these cells have
// neither a plain `function` (their output is level-sensitive-latch-based,
// not a combinational function of their inputs -- liberty models it as a
// `state_function` on an internal latch pin instead) nor an `ff()`/`latch()`
// block at the cell level (so they fall through both of LibertyLibrary's
// other extraction stages untouched) -- identity comes from the
// `clock_gating_integrated_cell` cell-level attribute and either a
// `pin_name_map` declaring the real pin names or, without one, the pins'
// own `clock_gate_{clock,enable,test,out}_pin` attributes. Restricted, deliberately, to cells whose
// `clock_gating_integrated_cell` value is exactly `"latch_posedge_precontrol"`
// (the one type this project has parsed and correctness-validated, via a
// liberty-text-derived behavioral model plus Yosys `equiv_induct` sequential
// induction, against real flip-flop usage -- see ClockGateInferencePass.cpp): a
// `"latch_negedge_precontrol"` cell gates on the WRONG edge for this flow's
// exclusively-posedge-triggered target FF cells (a real correctness risk, not a
// naming quirk), and a `"generic"` cell has unvalidated semantics -- both are
// silently excluded from the index rather than guessed at.
struct ICGCellPattern {
  std::string name;
  double area = 0.0;
  std::string clockPin;      // pin_name_map's "CK" slot
  std::string enablePin;     // pin_name_map's "E" slot
  std::string scanEnablePin; // pin_name_map's "SE" slot
  std::string outputPin;     // pin_name_map's "ECK" slot
};

// A two-output half-adder cell: exactly 2 input pins and exactly 2 output
// pins, one computing the SUM (a^b) and the other the CARRY (a&b).
// Multi-output cells are outside CellPattern's scope by construction
// (buildPattern() rejects a second output pin), and ABC drops them too --
// every Mio_CollectRoots* variant in ABC skips a gate with a `pTwin`, so
// `&nf` can never emit one, even though a half-adder cell is typically
// cheaper than the XOR2+AND2 pair it replaces. FuseHalfAdders recovers them
// with a post-mapping peephole; this struct is its oracle.
// A three-input, two-output FULL-adder cell: S = a^b^ci and
// CO = majority(a,b,ci).  Same reason as HalfAdderCellPattern for existing
// separately from CellPattern -- `Mio_CollectRootsNew2` skips every cell
// with a `pTwin`, so ABC's `&nf` can never emit one.
//
// WHY THIS MATTERS FOR TIMING, not just area.  On ibex the worst path is
// the PC/branch adder (`instr_rdata_id_o` -> `instr_addr_o[31]`).  Cut
// covering carries part of its ripple through full-adder cells and the
// rest through generic compound gates, because it only discovers an adder
// cell where the cut boundaries happen to line up, and the extra gates in
// the carry chain cost delay.  Building the chain out of adder cells by
// construction -- which is what a datapath synthesiser does -- is the fix,
// and this struct is its oracle.
struct FullAdderCellPattern {
  std::string name;
  double area = 0.0;
  // inputPins[0]/[1] are the two addend pins and inputPins[2] the carry-in.
  // All three are symmetric in BOTH outputs (xor and majority are fully
  // symmetric), so the caller may wire any operand to any pin -- but the
  // carry-in is kept last by convention so a chain reads naturally.
  llvm::SmallVector<std::string, 3> inputPins;
  std::string sumPin;   // function == a^b^ci
  std::string carryPin; // function == majority(a,b,ci)
};

struct HalfAdderCellPattern {
  std::string name;
  double area = 0.0;
  // inputPins[0]/[1] are the cell's two data pins, in liberty order. The
  // half-adder function is symmetric in both (a^b and a&b are both
  // commutative), so the pass may wire either operand to either pin.
  llvm::SmallVector<std::string, 2> inputPins;
  std::string sumPin;   // function == a^b
  std::string carryPin; // function == a&b
};

class LibertyLibrary {
public:
  // Parses `path` and indexes every purely-combinational cell with 1..6
  // input pins, exactly 1 output pin, and a parseable `function` string.
  // Cells with ff()/latch() blocks (sequential elements) are skipped for
  // this index, but ff()-bearing cells matching one of the 3 supported
  // shapes (see FFCellPattern) are separately indexed -- see lookupFF*().
  // Returns std::nullopt (with a diagnostic on stderr) on file-read failure;
  // per-cell parse failures are silently skipped (not every cell needs to
  // be matchable, only enough of them for the patterns we look for).
  static std::optional<LibertyLibrary> loadFromFile(llvm::StringRef path);

  // Looks up the cheapest (lowest-area) cell whose function matches `table`
  // over `arity` inputs, under any permutation of which leaf feeds which
  // cell input pin. Returns std::nullopt if no cell of that arity matches.
  // `inversionIsFree`: rank an inverted match on the cell's own area,
  // without charging an inverter. Only for callers that absorb an output
  // inversion into the NEXT gate's truth table (map-compressor-cells'
  // polarity-tracking prefix emitter); for everyone else the inverter is
  // real and the default pricing is correct.
  std::optional<CellMatch> lookup(unsigned arity, TruthTable table,
                                  bool inversionIsFree = false) const;

  // Cheapest (lowest-area) flip-flop cell with no clear/preset pin at all.
  const FFCellPattern *lookupFFNoReset() const { return cheapestNoResetFF; }
  // Cheapest flip-flop cell with an async clear (reset-to-0) pin only.
  const FFCellPattern *lookupFFClear() const { return cheapestClearFF; }
  // Cheapest flip-flop cell with an async preset (reset-to-1) pin only.
  const FFCellPattern *lookupFFSet() const { return cheapestSetFF; }

  // Cheapest real posedge-precontrol integrated clock-gating cell in this
  // liberty file (see ICGCellPattern), or nullptr if none qualify.
  const ICGCellPattern *lookupICGCell() const { return cheapestICG; }

  // Cheapest two-output half-adder cell (see HalfAdderCellPattern), or
  // nullptr if this liberty has none.
  const HalfAdderCellPattern *lookupHalfAdder() const {
    return cheapestHalfAdder;
  }

  // Cheapest three-input two-output full-adder cell, or nullptr if none.
  const FullAdderCellPattern *lookupFullAdder() const {
    return cheapestFullAdder;
  }

  size_t size() const { return cells.size(); }
  size_t numFFCells() const { return ffCells.size(); }
  size_t numICGCells() const { return icgCells.size(); }
  size_t numHalfAdderCells() const { return halfAdderCells.size(); }
  size_t numFullAdderCells() const { return fullAdderCells.size(); }

  // Names of every combinational cell of exactly `arity` inputs whose
  // truth table is EXACTLY `table` -- no permutation search, no
  // complement. Callers wanting the cheapest permuted/inverted match want
  // lookup() instead; this is for peepholes that must recognise an
  // existing instance's cell by function (FuseHalfAdders matches the AND2
  // and XOR2 halves of a half adder, both symmetric, so exact-match is
  // sufficient and avoids accepting an inverted cell by accident).
  llvm::SmallVector<llvm::StringRef, 8>
  cellNamesWithTable(unsigned arity, TruthTable table) const {
    llvm::SmallVector<llvm::StringRef, 8> out;
    for (const CellPattern &c : cells)
      if (c.arity() == arity && c.table == table)
        out.push_back(c.name);
    return out;
  }

  // The CellPattern for a named combinational cell, or nullptr if this
  // liberty has no such single-output combinational cell. Used by
  // post-mapping passes that must read an EXISTING instance's function
  // (ResubMapped walks a mapped netlist and needs each cell's truth
  // table to propagate functions through structural cuts).
  const CellPattern *patternFor(llvm::StringRef name) const {
    for (const CellPattern &c : cells)
      if (c.name == name)
        return &c;
    return nullptr;
  }

  // Area of a named combinational cell, or 0.0 if unknown.
  double areaOf(llvm::StringRef name) const {
    for (const CellPattern &c : cells)
      if (c.name == name)
        return c.area;
    return 0.0;
  }

  // Names of every single-input combinational cell whose function is the
  // identity of its input (truth table 0b10 under the bit-`a`-is-output-
  // for-assignment-`a` convention above) -- i.e. buffers. Liberty-driven,
  // deliberately NOT a name-pattern match; used by
  // RemoveIdentityBuffersPass. The returned StringRefs point into this
  // library's own cell storage and are valid for its lifetime.
  llvm::SmallVector<llvm::StringRef, 8> identityCellNames() const {
    llvm::SmallVector<llvm::StringRef, 8> out;
    for (const auto &c : cells)
      if (c.arity() == 1 && c.table == 0b10)
        out.push_back(c.name);
    return out;
  }

private:
  std::vector<CellPattern> cells;
  // Index: (arity, table) -> pointer into `cells` of the cheapest cell
  // whose OWN declared pin order produces exactly that table (no
  // permutation applied yet -- permutations are tried at lookup() time).
  // A real std::pair key, not a bit-packed uint64_t: with TruthTable
  // widened to 6 inputs, `table` alone can already occupy all 64 bits,
  // leaving no room to also pack `arity` into the same word.
  llvm::DenseMap<std::pair<unsigned, TruthTable>, const CellPattern *>
      exactIndex;

  std::vector<FFCellPattern> ffCells;
  // Area of the cheapest inverter, charged to an inverted-data flop: the
  // worst case, since ABC usually absorbs the inverter into D's logic.
  double inverterArea = 0.0;
  const FFCellPattern *cheapestNoResetFF = nullptr;
  const FFCellPattern *cheapestClearFF = nullptr;
  const FFCellPattern *cheapestSetFF = nullptr;

  std::vector<ICGCellPattern> icgCells;
  const ICGCellPattern *cheapestICG = nullptr;

  std::vector<HalfAdderCellPattern> halfAdderCells;
  std::vector<FullAdderCellPattern> fullAdderCells;
  const HalfAdderCellPattern *cheapestHalfAdder = nullptr;
  const FullAdderCellPattern *cheapestFullAdder = nullptr;

  void indexCell(const CellPattern &cell);
  void indexFFCell(const FFCellPattern &cell);
  void indexICGCell(const ICGCellPattern &cell);
};

} // namespace circt_abc

#endif // CIRCT_ABC_LIBERTY_LIBRARY_H

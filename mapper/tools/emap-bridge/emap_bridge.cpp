//===- emap_bridge.cpp ---------------------------------------------------===//
//
// A standalone mapper front-end that maps an AIGER file to liberty standard
// cells using Mockturtle's `emap`, as an alternative to ABC's `&nf`.
//
// WHY. Two limits of ABC's mapper were established from its source, and
// both are structural rather than tuning:
//   * the cell set is hard-capped at 6 inputs
//     (`Mio_CollectRootsNewDefault2( 6, ... )`, giaNf.c:320), while real
//     libraries stock many cells of 7..10 inputs (wide AOI/OAI and muxes)
//     that commercial synthesis uses heavily;
//   * every cell-set builder drops multi-output cells
//     (`if ( pGate->pTwin ) // skip large and multi-output`), so full and
//     half adders can never be selected.
// `emap` (Tempia Calvino & De Micheli, ICCAD'23; Mockturtle) removes both:
// it supports large cells and 2-output cells, and reports 7.48% average
// area against ABC, of which 2.41% is specifically the multi-output cells.
//
// The library is fed as a genlib written by ABC itself
// (`read_lib -X *MV* <liberty>; write_genlib`), which -- usefully -- emits
// the wide and multi-output cells ABC then refuses to use, expressed in
// the duplicate-name form Mockturtle's reader expects. Using ABC's own
// genlib keeps the cell subset (the *MV* ban) identical between the two
// mappers, so a comparison isolates the mapper.
//
//===----------------------------------------------------------------------===//

#include <cstdint>
#include <type_traits>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <lorina/aiger.hpp>
#include <lorina/genlib.hpp>
#include <mockturtle/algorithms/aig_balancing.hpp>
#include <mockturtle/algorithms/aig_resub.hpp>
#include <mockturtle/algorithms/cleanup.hpp>
#include <mockturtle/algorithms/node_resynthesis/sop_factoring.hpp>
#include <mockturtle/algorithms/node_resynthesis/xag_npn.hpp>
#include <mockturtle/algorithms/resubstitution.hpp>
#include <mockturtle/algorithms/sim_resub.hpp>
#include <mockturtle/algorithms/rewrite.hpp>
#include <mockturtle/utils/tech_library.hpp>
#include <mockturtle/views/depth_view.hpp>
#include <mockturtle/views/fanout_view.hpp>
#include <mockturtle/algorithms/refactoring.hpp>
#include <mockturtle/algorithms/emap.hpp>
#include <mockturtle/io/aiger_reader.hpp>
#include <mockturtle/io/genlib_reader.hpp>
#include <mockturtle/io/write_verilog.hpp>
#include <mockturtle/networks/aig.hpp>
#include <mockturtle/networks/block.hpp>
#include <mockturtle/utils/name_utils.hpp>
#include <mockturtle/utils/tech_library.hpp>
#include <mockturtle/views/cell_view.hpp>
#include <mockturtle/views/names_view.hpp>

// Real libraries stock cells of up to 10 inputs; the template bound must
// cover them or they are silently dropped exactly as ABC drops them.
static constexpr unsigned kMaxCellInputs = 10;

static void usage() {
  std::cerr
      << "usage: emap-bridge <in.aig> <lib.genlib> <out.v>"
         " [--area] [--no-multioutput] [--no-balance]\n"
         "  --area            area-oriented mapping (default: delay-oriented"
         " with area recovery, matching ABC's &nf)\n"
         "  --no-multioutput  disable 2-output cell mapping (FA/HA)\n"
         "  --no-balance      skip the pre-mapping AIG balancing\n"
         "  --cut-limit=N     cuts kept per node (emap default 16; &nf uses 32)\n"
         "  --required=<ps>   explicit required time; makes --relax live\n"
         "  --matching=MODE   boolean | structural | hybrid (default hybrid)\n"
         "  --sim-resub       simulation-guided resub in --mt-opt (stronger)\n"
         "  --balance-levels  balance for DEPTH in --mt-opt (default: size)\n"
         "  --mo-delay-tol=<x> slack when checking a single-output stand-in for a\n"
         "                    multi-output cell's pin delay (default 0 = upstream)\n"
         "  --resub-pis=N     resub window input count (problem size, default 8)\n"
         "  --resub-divisors=N  candidate divisors per node (default 150)\n"
         "  --resub-window=N  don't-care window size (default 12)\n"
         "  --dont-cares      enable don't-care-based resub (default off)\n"
         "  --preserve-depth  forbid resub substitutions that increase depth\n"
         "  --max-inserts=N   resub insert budget in --mt-opt (default 2)\n"
         "  --refactor[=N]    N rounds of factored-form refactoring before"
         " mapping (default 1)\n";
}

int main(int argc, char **argv) {
  if (argc < 4) {
    usage();
    return 1;
  }
  const std::string aigPath = argv[1], libPath = argv[2], outPath = argv[3];
  bool areaOriented = false, multiOutput = true, balance = true;
  unsigned refactorRounds = 0;
  unsigned mtRounds = 0;
  unsigned cutSize = 10;
  bool removeDominated = false, removeOverlapping = false;
  // emap tuning, all defaulted to emap_params' own values.
  unsigned areaFlowRounds = 3, elaRounds = 2, eswpRounds = 0;
  double relaxRequired = 0.0;
  // Levers that emap_params exposes but this bridge never did.
  unsigned cutLimit = 0;          // 0 = leave emap's default (16)
  double requiredTime = 0.0;      // 0 = "best possible" (emap's default)
  int matchingMode = -1;          // -1 = leave hybrid; 0 boolean, 1 structural
  bool simResub = false;          // stronger than window-based aig_resubstitution2
  unsigned maxInserts = 2;        // resubstitution insert budget
  // The --mt-opt loop balances with minimize_levels=false, i.e. purely for
  // size. That is a plausible source of the depth the emap netlist carries
  // against `&nf` (a deeper p90 path depth on ibex), so make it selectable.
  bool balanceLevels = false;
  // PHYS-MAP: emap only accepts a multi-output cell if some single-output cell
  // with the same function is <= it on EVERY pin.  In a real library that can
  // reject every full/half adder: an XOR3's worst pin may be a hair slower than
  // the full adder's S -- inside the error of the NLDM extraction behind the
  // timed genlib.  Allow a tolerance so the comparison is not made finer than
  // the data supports.
  double moDelayTol = 0.0;
  // Resubstitution PROBLEM SIZE, all at Mockturtle defaults until now:
  // max_pis is the reconvergence-driven window's input count (how many gates
  // are considered together), max_divisors the candidate pool. Don't-care
  // optimisation is off by default, and so is depth preservation -- the
  // latter matters here because emap's covering is intrinsically deeper than
  // `&nf`'s (ibex residual max 57-59 against 27).
  unsigned resubPis = 8, resubDivisors = 150, resubWindow = 12;
  bool dontCares = false, preserveDepth = false;
  for (int i = 4; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--area") areaOriented = true;
    else if (a == "--no-multioutput") multiOutput = false;
    else if (a == "--no-balance") balance = false;
    else if (a.rfind("--cut=", 0) == 0)
      cutSize = std::stoul(a.substr(6));
    else if (a == "--rm-dominated") removeDominated = true;
    else if (a == "--rm-overlapping") removeOverlapping = true;
    else if (a.rfind("--mt-opt=", 0) == 0)
      mtRounds = std::stoul(a.substr(9));
    else if (a.rfind("--area-flow=", 0) == 0)
      areaFlowRounds = std::stoul(a.substr(12));
    else if (a.rfind("--ela=", 0) == 0)
      elaRounds = std::stoul(a.substr(6));
    else if (a.rfind("--eswp=", 0) == 0)
      eswpRounds = std::stoul(a.substr(7));
    else if (a.rfind("--relax=", 0) == 0)
      relaxRequired = std::stod(a.substr(8));
    else if (a.rfind("--refactor", 0) == 0)
      refactorRounds = (a.size() > 11 && a[10] == '=')
                           ? static_cast<unsigned>(std::stoul(a.substr(11)))
                           : 1;
    else if (a.rfind("--cut-limit=", 0) == 0)
      cutLimit = std::stoul(a.substr(12));
    else if (a.rfind("--required=", 0) == 0)
      requiredTime = std::stod(a.substr(11));
    else if (a == "--matching=boolean") matchingMode = 0;
    else if (a == "--matching=structural") matchingMode = 1;
    else if (a == "--matching=hybrid") matchingMode = 2;
    else if (a == "--sim-resub") simResub = true;
    else if (a == "--balance-levels") balanceLevels = true;
    else if (a.rfind("--mo-delay-tol=", 0) == 0)
      moDelayTol = std::stod(a.substr(15));
    else if (a.rfind("--resub-pis=", 0) == 0)
      resubPis = std::stoul(a.substr(12));
    else if (a.rfind("--resub-divisors=", 0) == 0)
      resubDivisors = std::stoul(a.substr(17));
    else if (a.rfind("--resub-window=", 0) == 0)
      resubWindow = std::stoul(a.substr(15));
    else if (a == "--dont-cares") dontCares = true;
    else if (a == "--preserve-depth") preserveDepth = true;
    else if (a.rfind("--max-inserts=", 0) == 0)
      maxInserts = std::stoul(a.substr(14));
    else { usage(); return 1; }
  }

  std::vector<mockturtle::gate> gates;
  std::ifstream libIn(libPath);
  if (!libIn) {
    std::cerr << "emap-bridge: cannot open genlib " << libPath << "\n";
    return 1;
  }
  if (lorina::read_genlib(libIn, mockturtle::genlib_reader(gates)) !=
      lorina::return_code::success) {
    std::cerr << "emap-bridge: failed to parse genlib " << libPath << "\n";
    return 1;
  }

  mockturtle::tech_library_params tps;
  tps.multioutput_delay_tolerance = (float)moDelayTol;
  tps.ignore_symmetries = false;
  tps.verbose = false;
  mockturtle::tech_library<kMaxCellInputs> lib(gates, tps);

  mockturtle::names_view<mockturtle::aig_network> aig;
  if (lorina::read_aiger(aigPath, mockturtle::aiger_reader(aig)) !=
      lorina::return_code::success) {
    std::cerr << "emap-bridge: failed to read AIGER " << aigPath << "\n";
    return 1;
  }

  if (balance) {
    // Removes structural redundancy the AIG carries in from ABC; the
    // reference emap driver does the same before mapping.
    mockturtle::aig_balancing_params bps;
    bps.minimize_levels = false;
    bps.fast_mode = true;
    mockturtle::aig_balance(aig, bps);
  }

  // Mockturtle-native AIG optimisation. The published emap results are
  // measured on AIGs optimised by Mockturtle's own engines, not ABC's, so
  // this closes the last methodological difference between our chain and
  // the papers': balancing, NPN-database rewriting (the 4-input exact
  // library), and window-based resubstitution.
  for (unsigned r = 0; r < mtRounds; ++r) {
    mockturtle::aig_balancing_params bps2;
    bps2.minimize_levels = balanceLevels;
    bps2.fast_mode = false;
    mockturtle::aig_balance(aig, bps2);

    mockturtle::xag_npn_resynthesis<mockturtle::aig_network> npn;
    mockturtle::exact_library<mockturtle::aig_network> exactLib(npn);
    mockturtle::rewrite_params rwps;
    mockturtle::rewrite(aig, exactLib, rwps);
    aig = mockturtle::cleanup_dangling(aig);

    mockturtle::resubstitution_params rps2;
    rps2.max_pis = resubPis;
    rps2.max_divisors = resubDivisors;
    rps2.max_inserts = maxInserts;
    rps2.use_dont_cares = dontCares;
    rps2.window_size = resubWindow;
    rps2.preserve_depth = preserveDepth;
    rps2.progress = false;
    if (simResub) {
      // Simulation-guided resubstitution: finds substitutions the
      // window-based engine cannot, at a runtime cost.
      mockturtle::sim_resubstitution(aig, rps2);
    } else {
      mockturtle::depth_view daig{aig};
      mockturtle::fanout_view faig{daig};
      mockturtle::aig_resubstitution2(faig, rps2);
    }
    aig = mockturtle::cleanup_dangling(aig);
  }

  // Factored-form refactoring. The number of literals in a factored form
  // correlates with CMOS transistor count far better than AIG node count
  // does, which is the premise of DAC'23 "Improving Standard-Cell Design
  // Flow using Factored Form Optimization" (+2.8% area after mapping over
  // a high-effort area-oriented baseline). That paper's optimizer is NOT
  // public; what IS public in Mockturtle is the cost function
  // (properties/litcost.hpp) and an SOP-factoring resynthesis with the
  // same inverter-aware cost. This wires those two together as an
  // approximation of the published method -- report it as such, not as a
  // reproduction of the paper.
  for (unsigned r = 0; r < refactorRounds; ++r) {
    mockturtle::sop_factoring_params fps;
    fps.consider_inverter_cost = true;
    mockturtle::sop_factoring<mockturtle::names_view<mockturtle::aig_network>>
        resyn(fps);
    mockturtle::refactoring_params rps;
    // Conservative: max_pis 10 with don't cares crashes inside
    // refactoring on this design, so keep the library's own defaults.
    rps.max_pis = 6;
    rps.use_dont_cares = false;
    mockturtle::refactoring(aig, resyn, rps);
    aig = mockturtle::cleanup_dangling(aig);
  }

  mockturtle::emap_params ps;
  ps.matching_mode = mockturtle::emap_params::hybrid;
  ps.area_oriented_mapping = areaOriented;
  ps.map_multioutput = multiOutput;
  // Area-recovery effort. `relax_required` trades delay for area, the right
  // trade here: the mapper's own delay estimate sits well inside the clock
  // target, so its delay constraint is slack by construction.
  ps.area_flow_rounds = areaFlowRounds;
  ps.ela_rounds = elaRounds;
  ps.eswp_rounds = eswpRounds;
  ps.relax_required = relaxRequired;
  ps.remove_dominated_cuts = removeDominated;
  ps.remove_overlapping_multicuts = removeOverlapping;
  // emap keeps 16 cuts per node by default; ABC's `&nf` runs the production
  // recipe with `-C 32`, i.e. TWICE the budget, so the default was mapping
  // with half the search ABC gets.
  if (cutLimit)
    ps.cut_enumeration_ps.cut_limit = cutLimit;
  // `relax_required` is a percentage OF `required_time`, so with the default
  // required_time == 0 ("best possible") it has nothing to relax and measures
  // as a no-op -- which is exactly what the first sweep found. Setting a real
  // target makes it live.
  if (requiredTime > 0.0)
    ps.required_time = requiredTime;
  if (matchingMode == 0)
    ps.matching_mode = mockturtle::emap_params::boolean;
  else if (matchingMode == 1)
    ps.matching_mode = mockturtle::emap_params::structural;
  else if (matchingMode == 2)
    ps.matching_mode = mockturtle::emap_params::hybrid;
  mockturtle::emap_stats st;
  // NOTE: emap's first template parameter is the CUT size (default 6), not
  // the cell-input bound -- NInputs is deduced from the tech_library. A cut
  // smaller than the widest cell makes those cells unreachable.
  auto mapWith = [&](auto tag) { return mockturtle::emap<decltype(tag)::value>(aig, lib, ps, &st); };
  mockturtle::cell_view<mockturtle::block_network> res =
      cutSize <= 6   ? mapWith(std::integral_constant<unsigned, 6>{})
      : cutSize <= 8 ? mapWith(std::integral_constant<unsigned, 8>{})
      : cutSize <= 10 ? mapWith(std::integral_constant<unsigned, 10>{})
                      : mapWith(std::integral_constant<unsigned, 12>{});

  mockturtle::names_view resNames{res};
  mockturtle::restore_network_name(aig, resNames);
  mockturtle::restore_pio_names_by_order(aig, resNames);
  mockturtle::write_verilog_with_cell(resNames, outPath);

  std::cout << "[emap-bridge] gates=" << aig.num_gates()
            << " area=" << res.compute_area()
            << " delay=" << res.compute_worst_delay()
            << " multioutput_cells=" << st.multioutput_gates
            << (areaOriented ? " (area-oriented)" : " (delay-oriented)")
            << "\n";
  return 0;
}

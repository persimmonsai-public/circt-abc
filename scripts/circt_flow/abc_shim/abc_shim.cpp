// Thin extern "C" shim exposing ABC's real in-process entry points
// (Abc_Start/Cmd_CommandExecute/Abc_Stop) for ctypes consumption from
// Python -- the "shallow" ABC-Python integration: run ABC command scripts
// in-process instead of via a subprocess, with no changes to ABC or to
// mapper/lib/ABCBridge.cpp's existing subprocess-based invocation.
//
// Precedent: Yosys's own passes/techmap/abc.cc and abc9_exe.cc already
// call abc::Abc_RealMain(...) in-process, in this exact codebase.
//
// ABC's headers wrap every declaration in `namespace abc { ... }` when
// built with -DABC_NAMESPACE=abc (see third_party/yosys/build's own
// compile_commands.json for libyosys-abc.dir), which is why every symbol
// this file calls is C++-mangled, not plain C -- hence this shim. It must
// be compiled with the SAME defines used to build
// third_party/yosys/build/libyosys-abc.a (see build.sh in this directory)
// and linked against that static library.
//
// "Deep tier" milestone 1 of the ABC-Python integration (network.py is its
// Python side): read-only inspection of the CURRENT
// network in the global ABC frame -- object kind, fanins, fanouts, and
// (for a mapped node) its assigned liberty cell name. These functions are
// deliberately read-only: they never mutate the network, never change
// what recipe runs or what gets written out, and are scoped to exactly
// the Abc_Ntk_t/Abc_Obj_t accessors ABC's OWN base/io/ioWriteVerilog.c
// uses to emit a mapped netlist (Abc_ObjFanin/Abc_ObjFanout/Abc_ObjName/
// Mio_GateReadName on pObj->pData cast to Mio_Gate_t*) -- mirroring the
// exact ground-truth mechanism `write_verilog` itself relies on, rather
// than reverse-engineering a parallel path, is what makes the milestone's
// write_verilog-text cross-check (see abc_shim_verify.py) a meaningful
// check instead of two independently-guessed implementations agreeing by
// luck.
//
// Unlike Abc_Start/Cmd_CommandExecute (genuinely part of ABC's own
// advertised top-level entry-point surface), Abc_Obj_t's field layout is
// NOT a stable public API -- it can change across an ABC revision with no
// deprecation notice. Treat any future third_party/ABC_REV bump as a
// reason to re-verify this file, not just recompile it.

#include "base/main/main.h"
#include "map/mio/mio.h"

#include <cstring>

extern "C" {

void abc_shim_start() { abc::Abc_Start(); }

void abc_shim_stop() { abc::Abc_Stop(); }

// Runs a `;`-separated ABC command script against the global ABC frame,
// exactly as `abc -q "<script>"` does for the caller in
// mapper/lib/ABCBridge/ABCBridge.cpp. Returns 0 on success, matching
// Cmd_CommandExecute's own convention.
int abc_shim_execute(const char *command) {
  abc::Abc_Frame_t *frame = abc::Abc_FrameGetGlobalFrame();
  return abc::Cmd_CommandExecute(frame, command);
}

// --- Read-only network inspection (deep tier, milestone 1) ------------

namespace {

// The network currently held by the global ABC frame, or nullptr if none
// has been read/created yet.
abc::Abc_Ntk_t *currentNtk() {
  abc::Abc_Frame_t *frame = abc::Abc_FrameGetGlobalFrame();
  return frame ? abc::Abc_FrameReadNtk(frame) : nullptr;
}

// Object at index `i`, or nullptr if `i` is out of range or names a
// deleted slot -- ABC's own Abc_NtkForEachObj macro explicitly skips
// NULL entries in the same vObjs array (deleted objects leave a hole,
// their id is not reused until compaction), so every accessor below must
// guard against it exactly like every real ABC pass does.
abc::Abc_Obj_t *objAt(abc::Abc_Ntk_t *ntk, int i) {
  if (!ntk || i < 0 || i >= abc::Abc_NtkObjNumMax(ntk))
    return nullptr;
  return abc::Abc_NtkObj(ntk, i);
}

void copyToBuf(const char *s, char *buf, int buflen) {
  if (buflen <= 0)
    return;
  if (!s) {
    buf[0] = '\0';
    return;
  }
  std::strncpy(buf, s, buflen - 1);
  buf[buflen - 1] = '\0';
}

} // namespace

// Total object slots in the current network (including any deleted
// holes -- callers should skip an index if e.g. abc_shim_obj_is_node/
// abc_shim_obj_is_pi/abc_shim_obj_is_po all return 0 AND
// abc_shim_obj_name returns an empty string for it). Returns 0 if no
// network is current.
int abc_shim_num_objects() {
  abc::Abc_Ntk_t *ntk = currentNtk();
  return ntk ? abc::Abc_NtkObjNumMax(ntk) : 0;
}

int abc_shim_obj_is_pi(int i) {
  abc::Abc_Obj_t *obj = objAt(currentNtk(), i);
  return obj && abc::Abc_ObjIsPi(obj) ? 1 : 0;
}

int abc_shim_obj_is_po(int i) {
  abc::Abc_Obj_t *obj = objAt(currentNtk(), i);
  return obj && abc::Abc_ObjIsPo(obj) ? 1 : 0;
}

int abc_shim_obj_is_node(int i) {
  abc::Abc_Obj_t *obj = objAt(currentNtk(), i);
  return obj && abc::Abc_ObjIsNode(obj) ? 1 : 0;
}

int abc_shim_obj_num_fanins(int i) {
  abc::Abc_Obj_t *obj = objAt(currentNtk(), i);
  return obj ? abc::Abc_ObjFaninNum(obj) : 0;
}

// Fanin object id at position `which` (0-based, matching liberty pin
// order for a mapped node -- see Mio_GateReadPins in ABC's own
// ioWriteVerilog.c, which walks fanins in this exact same order), or -1
// if `i`/`which` is out of range.
int abc_shim_obj_fanin(int i, int which) {
  abc::Abc_Obj_t *obj = objAt(currentNtk(), i);
  if (!obj || which < 0 || which >= abc::Abc_ObjFaninNum(obj))
    return -1;
  return static_cast<int>(abc::Abc_ObjId(abc::Abc_ObjFanin(obj, which)));
}

int abc_shim_obj_num_fanouts(int i) {
  abc::Abc_Obj_t *obj = objAt(currentNtk(), i);
  return obj ? abc::Abc_ObjFanoutNum(obj) : 0;
}

// Fanout object id at position `which` (0-based, ABC's own internal
// fanout order -- not necessarily stable across ABC revisions, unlike
// fanin order which liberty pin order pins down), or -1 if out of range.
int abc_shim_obj_fanout(int i, int which) {
  abc::Abc_Obj_t *obj = objAt(currentNtk(), i);
  if (!obj || which < 0 || which >= abc::Abc_ObjFanoutNum(obj))
    return -1;
  return static_cast<int>(abc::Abc_ObjId(abc::Abc_ObjFanout(obj, which)));
}

// Object's net name (empty string if unnamed or `i` invalid).
void abc_shim_obj_name(int i, char *buf, int buflen) {
  abc::Abc_Obj_t *obj = objAt(currentNtk(), i);
  copyToBuf(obj ? abc::Abc_ObjName(obj) : nullptr, buf, buflen);
}

// For a MAPPED node, the assigned liberty cell's name (e.g.
// "AOI21_X2"); empty string if `i` is invalid, not a
// node, or the current network isn't gate-mapped (Abc_NtkHasMapping ==
// false) -- mirrors exactly the access pattern ABC's own
// base/io/ioWriteVerilog.c uses ((Mio_Gate_t*)pObj->pData, then
// Mio_GateReadName), so this answer is definitionally what
// `write_verilog` itself would print for this cell.
void abc_shim_obj_liberty_cell_name(int i, char *buf, int buflen) {
  abc::Abc_Ntk_t *ntk = currentNtk();
  abc::Abc_Obj_t *obj = objAt(ntk, i);
  if (!obj || !abc::Abc_ObjIsNode(obj) || !ntk || !abc::Abc_NtkHasMapping(ntk) ||
      !obj->pData) {
    copyToBuf(nullptr, buf, buflen);
    return;
  }
  abc::Mio_Gate_t *gate = static_cast<abc::Mio_Gate_t *>(obj->pData);
  copyToBuf(abc::Mio_GateReadName(gate), buf, buflen);
}

// --- Cheap structural read-outs for recipe experimentation (deep tier,
// milestone 2) -- same "current network in the global frame" pattern as
// milestone 1 above, using ABC's own stable top-level Abc_Ntk* query
// functions (base/abc/abc.h) rather than any new struct traversal. All
// four return 0 if no network is current, matching abc_shim_num_objects'
// own convention.

int abc_shim_num_nodes() {
  abc::Abc_Ntk_t *ntk = currentNtk();
  return ntk ? abc::Abc_NtkNodeNum(ntk) : 0;
}

// Real DFS-based level computation (abc/src/base/abc/abcDfs.c) -- the
// same one ABC's own "print_stats"/"ps" command reports.
int abc_shim_num_levels() {
  abc::Abc_Ntk_t *ntk = currentNtk();
  return ntk ? abc::Abc_NtkLevel(ntk) : 0;
}

int abc_shim_num_pis() {
  abc::Abc_Ntk_t *ntk = currentNtk();
  return ntk ? abc::Abc_NtkPiNum(ntk) : 0;
}

int abc_shim_num_pos() {
  abc::Abc_Ntk_t *ntk = currentNtk();
  return ntk ? abc::Abc_NtkPoNum(ntk) : 0;
}

} // extern "C"

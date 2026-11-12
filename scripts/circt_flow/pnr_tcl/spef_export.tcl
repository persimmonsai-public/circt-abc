# SPEF export for an OpenROAD P&R stage (circt_flow/spef_pincap.py explains
# why; circt_flow/pnr_recipe.py sets the environment). Source this file from
# the pack's export script and call one of
#
#   spef_export_global_route out.spef    estimate_parasitics -global_routing,
#                                        SPEF written, estimator debug lines
#                                        bracketed by markers on stdout
#   spef_export_placement out.spef       estimate_parasitics -placement (exact,
#                                        nothing to restore)
#
# OpenROAD's SPEF writer leaves the capacitance held on pin nodes out of the
# *CAP lines of a global-route SPEF (the *D_NET total keeps it), so OpenSTA
# reads less wire cap than the database holds. The estimator's debug
# lines (`EST est_rc`) say where that cap sits. OpenROAD prints them on stdout
# only (log_begin does not capture them), so the procedure brackets them with
#   SPEF_EXPORT_BEGIN <path>   /   SPEF_EXPORT_END <path>
# lines (<path> is the SPEF's normalized path) and then prints
#   SPEF_EXPORT_FILE <size> <crc32> <path>
# for the file it has just written; the export's stdout log has to be kept
# (the usual `> run.log`). After the pack's command, stages.py restores a
# SPEF below the output directory only when a `*.log` there holds these lines
# for its path and the file still has that size and CRC-32
# (circt_flow/spef_pincap.py restore_exports); by hand:
#   python3 -m circt_flow.spef_pincap restore <spef> --est-rc-log <that log>
#
# Variables (optional):
#   SPEF_RESTORE_PINCAP=0   no debug lines (they run to many lines per net); the SPEF
#                           is written as OpenROAD writes it
#                           (pnr.spef_restore_pincap: false)

proc spef_export_global_route {spef} {
  if {[info exists ::env(SPEF_RESTORE_PINCAP)] && $::env(SPEF_RESTORE_PINCAP) eq "0"} {
    estimate_parasitics -global_routing -spef_file $spef
    return
  }
  set path [file normalize $spef]
  puts "SPEF_EXPORT_BEGIN $path"
  set_debug_level EST est_rc 1
  estimate_parasitics -global_routing -spef_file $spef
  set_debug_level EST est_rc 0
  puts "SPEF_EXPORT_END $path"
  if {[file isfile $path]} {
    puts [format "SPEF_EXPORT_FILE %d %08x %s" [file size $path] \
            [spef_export_crc32 $path] $path]
  }
}

# CRC-32 of a file, read in pieces (Tcl's zlib crc32 is the standard CRC-32).
proc spef_export_crc32 {path} {
  set f [open $path rb]
  set crc 0
  while {1} {
    set data [read $f 1048576]
    if {$data eq ""} { break }
    set crc [zlib crc32 $data $crc]
  }
  close $f
  return $crc
}

proc spef_export_placement {spef} {
  estimate_parasitics -placement -spef_file $spef
}

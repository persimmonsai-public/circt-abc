configure_lit_site_cfg(
  ${CMAKE_CURRENT_SOURCE_DIR}/lit.site.cfg.py.in
  ${CMAKE_CURRENT_BINARY_DIR}/lit.site.cfg.py
  MAIN_CONFIG
  ${CMAKE_CURRENT_SOURCE_DIR}/lit.cfg.py
  )

# FileCheck / count / not come from the LLVM build this project was
# configured against; they are not built here. They are listed as deps only
# when they exist as targets, which they do not in an out-of-tree build --
# hence the bare tool dependency on circt-abc-opt alone.
set(CIRCT_ABC_TEST_DEPENDS
  circt-abc-opt
  )

add_lit_testsuite(check-circt-abc "Running the circt-abc regression tests"
  ${CMAKE_CURRENT_BINARY_DIR}
  DEPENDS ${CIRCT_ABC_TEST_DEPENDS}
  )
set_target_properties(check-circt-abc PROPERTIES FOLDER "Tests")

add_lit_testsuites(CIRCT_ABC ${CMAKE_CURRENT_SOURCE_DIR}
  DEPENDS ${CIRCT_ABC_TEST_DEPENDS}
  )

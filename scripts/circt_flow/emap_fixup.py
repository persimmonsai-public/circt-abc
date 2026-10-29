#!/usr/bin/env python3
"""Make Mockturtle `emap`'s Verilog re-importable by the flow.

emap's writer differs from ABC's in three ways that all break
`--abc-use-mapped-dir`:

1. The module is always named `top`; the bridge expects the residual
   target's own name.
2. Identifiers carrying a bit index are written bare -- `foo_in0[0]` --
   where ABC writes them as escaped identifiers, `\\foo_in0[0] `. Bare, the
   `[0]` parses as a bit-select, so the port list is a syntax error
   ("expected ';'", "'foo_in0' is not a type").
3. Constant drivers are emitted as instances of `_const0_` / `_const1_`,
   which are not cells in any liberty ("unknown module '_const0_'"). ABC
   writes plain `assign` statements instead.

(3) is the one that actually stops the import: with the const cells rewritten
the 367 KB netlist parses with 0 errors, and the line-1 diagnostics that look
like a broken port list turn out to be slang error-recovery cascade.

All three are textual; nothing about the mapping changes.
"""
import re
import sys


ESCAPE = re.compile(r'(?<![\\\w$])([A-Za-z_][\w$]*\[\d+(?::\d+)?\])')
CONST = re.compile(
    r'^[ \t]*_const([01])_\s+\S+\s*\(\s*\.\w+\s*\(\s*(\S+)\s*\)\s*\)\s*;',
    re.M)


def fixup(text, module):
    text = re.sub(r'\bmodule\s+top\b', 'module ' + module, text, count=1)
    # Escaped identifiers must end with a space; the negative lookbehind
    # keeps already-escaped names from being escaped twice.
    text = ESCAPE.sub(lambda m: '\\' + m.group(1) + ' ', text)
    # `_const0_ g(.z(x));` -> `assign x = 1'b0;`
    text = CONST.sub(lambda m: "  assign %s = 1'b%s;" % (m.group(2), m.group(1)),
                     text)
    return text


def main():
    if len(sys.argv) != 4:
        print('usage: emap_fixup.py <in.v> <module-name> <out.v>', file=sys.stderr)
        return 2
    src, module, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    text = open(src).read()
    out = fixup(text, module)
    open(dst, 'w').write(out)
    n = len(ESCAPE.findall(text))
    c = len(CONST.findall(text))
    print('emap-fixup: module -> %s, escaped %d bit-indexed identifier(s), '
          '%d _const cell(s) -> assign' % (module, n, c))
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Output-pin names per cell, read from the liberty files.

A hardcoded output-pin list is not safe here: a library may have cells whose
output is not named like Y or Q (a carry output, say), and every one of
those silently drops its fanout edges from a structural depth graph, which
truncates any path through it.  Derive the directions instead.
"""
import re, os, json, sys

# LIBPINS_LIBS (os.pathsep-separated) lists the liberties to read.
LIBS = (os.environ["LIBPINS_LIBS"].split(os.pathsep)
        if os.environ.get("LIBPINS_LIBS") else [])
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libpins.json")

def build():
    out = {}
    cell_re = re.compile(r'\bcell\s*\(\s*"?([A-Za-z_][\w]*)"?\s*\)\s*\{')
    pin_re  = re.compile(r'\b(?:pin|bundle)\s*\(\s*"?([A-Za-z_][\w]*)"?\s*\)\s*\{')
    dir_re  = re.compile(r'\bdirection\s*:\s*(\w+)')
    for lib in LIBS:
        if not os.path.exists(lib):
            continue
        txt = open(lib, errors='ignore').read()
        for m in cell_re.finditer(txt):
            name = m.group(1)
            # cell body: brace-match from the opening brace
            i = m.end() - 1
            depth_ = 0
            for j in range(i, len(txt)):
                if txt[j] == '{': depth_ += 1
                elif txt[j] == '}':
                    depth_ -= 1
                    if depth_ == 0: break
            body = txt[i:j]
            outs = []
            for pm in pin_re.finditer(body):
                k = pm.end() - 1
                d2 = 0
                for l in range(k, len(body)):
                    if body[l] == '{': d2 += 1
                    elif body[l] == '}':
                        d2 -= 1
                        if d2 == 0: break
                pbody = body[k:l]
                dm = dir_re.search(pbody)
                if dm and dm.group(1) == 'output':
                    outs.append(pm.group(1))
            if outs:
                out.setdefault(name, sorted(set(outs)))
    return out

def load():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    d = build()
    json.dump(d, open(CACHE, 'w'))
    return d

if __name__ == '__main__':
    d = build()
    json.dump(d, open(CACHE, 'w'))
    print("cells with outputs:", len(d))
    import collections
    print("distinct output pin names:",
          sorted({p for v in d.values() for p in v}))

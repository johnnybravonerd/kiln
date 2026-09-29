"""Kiln multi-file module resolution.

`use gfx` resolves `gfx.kn` or `gfx/mod.kn` from:
  1. the importing file's directory
  2. `lib/` next to kiln.py (the language standard library)

`pub` marks exports. Importers access them as `gfx.name` (or the `as` alias).
Cycle detection reports a clear import chain.
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field

from .errors import CompileError, SourceLoc
from .parser import parse
from . import ast_nodes as A

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STDLIB = os.path.join(HERE, "lib")


@dataclass
class LoadedModule:
    name: str                 # logical module path, e.g. "gfx"
    filename: str
    source: str
    prog: A.Program
    # pub exports: local name -> decl
    exports: dict = field(default_factory=dict)


def read_utf8(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except FileNotFoundError:
        raise CompileError(f"file not found: {path}")
    except UnicodeDecodeError:
        raise CompileError(f"{path} is not valid UTF-8 text")


def find_module_file(mod_path: str, search_dirs: list[str]) -> str | None:
    """Resolve `a.b.c` to a.b/c.kn, a/b/c.kn, a/b/c/mod.kn, etc."""
    parts = mod_path.split(".")
    candidates = []
    # flat: gfx.kn
    candidates.append(os.path.join(*parts) + ".kn")
    # nested dir: gfx/mod.kn
    candidates.append(os.path.join(*parts, "mod.kn"))
    # dotted as nested: a/b.kn for a.b
    if len(parts) > 1:
        candidates.append(os.path.join(*parts[:-1], parts[-1] + ".kn"))
    for d in search_dirs:
        for rel in candidates:
            full = os.path.normpath(os.path.join(d, rel))
            if os.path.isfile(full):
                return full
    return None


class ModuleLoader:
    def __init__(self, entry_path: str, lib_dirs: list[str] | None = None):
        self.entry_path = os.path.abspath(entry_path)
        self.entry_dir = os.path.dirname(self.entry_path)
        self.lib_dirs = lib_dirs or [STDLIB]
        self.modules: dict[str, LoadedModule] = {}
        self.loading: list[str] = []  # stack for cycle detection
        self.by_file: dict[str, LoadedModule] = {}

    def search_dirs_for(self, importer_file: str) -> list[str]:
        dirs = [os.path.dirname(os.path.abspath(importer_file))]
        # also entry dir (so sibling modules resolve from subdirs)
        if self.entry_dir not in dirs:
            dirs.append(self.entry_dir)
        for d in self.lib_dirs:
            if os.path.isdir(d) and d not in dirs:
                dirs.append(d)
        return dirs

    def load_entry(self) -> tuple[A.Program, list[LoadedModule]]:
        source = read_utf8(self.entry_path)
        prog = parse(source, self.entry_path)
        prog.filename = self.entry_path
        # entry module name is optional
        # Entry-file symbols stay unqualified (so `fn main` remains `main`) even if
        # the file declares `module app.foo` for documentation / future packaging.
        entry = LoadedModule(
            name="",
            filename=self.entry_path,
            source=source,
            prog=prog,
        )
        entry.declared_module = prog.module_name
        self.by_file[self.entry_path] = entry
        self._collect_uses(entry)
        # Qualify symbols in every loaded module
        all_mods = [entry] + [m for m in self.modules.values()]
        # dedupe entry if it registered itself
        seen = set()
        ordered = []
        for m in all_mods:
            key = m.filename
            if key in seen:
                continue
            seen.add(key)
            ordered.append(m)
            self._qualify(m)
        # Merge into one Program for the checker
        merged = self._merge(ordered)
        return merged, ordered

    def _collect_uses(self, mod: LoadedModule):
        for d in mod.prog.decls:
            if isinstance(d, A.UseStmt):
                self._import(d.path, mod.filename, d)

    def _import(self, path: str, importer_file: str, use_node: A.UseStmt):
        if path in self.loading:
            chain = " -> ".join(self.loading + [path])
            raise CompileError(f"circular module dependency: {chain}", use_node.loc)
        if path in self.modules:
            return
        found = find_module_file(path, self.search_dirs_for(importer_file))
        if found is None:
            searched = ", ".join(self.search_dirs_for(importer_file))
            raise CompileError(
                f"cannot find module `{path}` (looked for `{path}.kn` / `{path}/mod.kn` in: {searched})",
                use_node.loc,
            )
        found = os.path.abspath(found)
        if found == os.path.abspath(importer_file):
            raise CompileError(f"module `{path}` resolves to the same file that imports it", use_node.loc)
        if found in self.by_file:
            # already loaded under another name?
            existing = self.by_file[found]
            self.modules[path] = existing
            return
        self.loading.append(path)
        try:
            source = read_utf8(found)
            prog = parse(source, found)
            prog.filename = found
            # Prefer declared module name; else the use path
            declared = prog.module_name
            if declared and declared != path and not path.startswith(declared):
                # allow `module gfx` for file gfx.kn used as gfx
                if declared.split(".")[-1] != path.split(".")[-1]:
                    raise CompileError(
                        f"file `{found}` declares `module {declared}` but was imported as `{path}`",
                        use_node.loc,
                    )
            logical = path
            loaded = LoadedModule(name=logical, filename=found, source=source, prog=prog)
            self.modules[logical] = loaded
            self.by_file[found] = loaded
            self._collect_uses(loaded)
        finally:
            self.loading.pop()

    def _qualify(self, mod: LoadedModule):
        """Attach module / qual_name / exports on each decl."""
        prefix = mod.name  # "" for entry without module decl
        exports = {}
        for d in mod.prog.decls:
            if isinstance(d, (A.ModuleStmt, A.UseStmt)):
                continue
            if isinstance(d, A.FnDecl):
                d.module = prefix or None
                if prefix:
                    d.qual_name = f"{prefix}.{d.name}"
                else:
                    d.qual_name = d.name
                if d.is_pub or d.is_extern:
                    # externs are always callable by their bare C name from the owning module;
                    # pub marks re-exports to importers.
                    if d.is_pub:
                        exports[d.name] = d
                    elif d.is_extern and prefix:
                        # extern visible inside the module under bare name; not exported unless pub
                        pass
            elif isinstance(d, A.StructDecl):
                d.module = prefix or None
                if prefix:
                    d.qual_name = f"{prefix}.{d.name}"
                else:
                    d.qual_name = d.name
                if d.is_pub:
                    exports[d.name] = d
            elif isinstance(d, A.LetStmt):
                d.module = prefix or None
                if prefix:
                    d.qual_name = f"{prefix}.{d.name}"
                else:
                    d.qual_name = d.name
                if d.is_pub:
                    exports[d.name] = d
        mod.exports = exports

    def _merge(self, ordered: list[LoadedModule]) -> A.Program:
        """Produce a single Program: all module decls first (dependency order), then entry."""
        # topological-ish: modules dict insertion order follows first import; put entry last
        entry = ordered[0]
        others = [m for m in ordered[1:]]
        # Also include any modules only reachable — already in ordered
        merged = A.Program(loc=entry.prog.loc, filename=entry.filename,
                           module_name=entry.prog.module_name)
        # Keep use stmts on the entry so the checker can bind module names
        for m in others:
            for d in m.prog.decls:
                if isinstance(d, (A.ModuleStmt,)):
                    continue
                if isinstance(d, A.UseStmt):
                    # module-internal uses: keep so nested imports bind
                    merged.decls.append(d)
                    continue
                merged.decls.append(d)
        for d in entry.prog.decls:
            if isinstance(d, A.ModuleStmt):
                continue
            merged.decls.append(d)
        # Stash loader metadata for the checker
        merged._kiln_modules = {m.name: m for m in others if m.name}
        merged._kiln_entry = entry
        merged._kiln_all = ordered
        return merged


def load_program(entry_path: str) -> tuple[A.Program, str, list[LoadedModule]]:
    """Parse entry + resolve all `use` imports. Returns (merged_prog, entry_source, modules)."""
    loader = ModuleLoader(entry_path)
    merged, ordered = loader.load_entry()
    entry_source = ordered[0].source
    return merged, entry_source, ordered

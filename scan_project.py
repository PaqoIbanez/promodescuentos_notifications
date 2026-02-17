#!/usr/bin/env python3
"""
scan_project.py
Escanea un proyecto, detecta tecnología/lenguajes, respeta .gitignore,
y concatena el contenido de todos los archivos (no binarios) en
<nombre-del-proyecto>.txt. Antes de cada archivo, inserta un comentario
con la ruta relativa usando el estilo de comentario apropiado para ese
tipo de archivo.

NUEVO:
- Puedes definir patrones extra a ignorar en EXTRA_IGNORE_PATTERNS (abajo),
  con sintaxis tipo .gitignore (gitwildmatch), p. ej.:
    ".vscode/"
    "package-lock.json"
    "*.lock"
    "dist/"
- También puedes pasar patrones con --extra-ignore (repetible).

Uso:
    python scan_project.py /ruta/al/proyecto [--output-dir /otra/ruta]
    python scan_project.py . --extra-ignore ".vscode/" --extra-ignore "package-lock.json"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import fnmatch
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Set, Tuple, Dict, List

# -----------------------------------------------------------------------------
# Configurable: patrones extra a ignorar (además de .gitignore)
# Sintaxis: igual a .gitignore (gitwildmatch). Ejemplos:
#   ".vscode/"
#   "package-lock.json"
#   "*.lock"
#   "coverage/"
#   "reports/*.html"
# -----------------------------------------------------------------------------
EXTRA_IGNORE_PATTERNS: List[str] = [
    # Agrega aquí tus patrones personalizados:
    ".vscode/",
    "package-lock.json",
    ".editorconfig",
    "scan_project.py",
    ".gitignore",
    "wkf_03_DI_CARGA_INDICADORES.XML",
    "wkf_02_DI_CARGA_NARRATIVAS.XML",
    "wkf_01_DI_CARGA_CATALOGOS.XML",
    "wkf_03_DI_CARGA_INDICADORES_PPRD.XML",
    "wkf_03_DI_CARGA_INDICADORES_DEVL.XML",
    "wkf_02_DI_CARGA_NARRATIVAS_PPRD.XML",
    "wkf_02_DI_CARGA_NARRATIVAS_DEVL.XML",
    "wkf_01_DI_CARGA_CATALOGOS_PPRD.XML",
    "wkf_01_DI_CARGA_CATALOGOS_DEVL.XML",
    "extraction_devl.json",
    "extraction_pprd.json",
    "extraction.json",
    "extraction_20260120.json",
    "xml_inputs/",
    "deals_history.csv",
    "README.md"
]

# ----------------------------- .gitignore helpers -----------------------------

class IgnoreMatcher:
    """Best-effort matcher que usa pathspec si está disponible; si no, intenta
    'git check-ignore'; si no, cae a un matcher simple con fnmatch.
    Además soporta patrones extra definidos por el usuario.
    """

    def __init__(self, root: Path, extra_patterns: Optional[List[str]] = None):
        self.root = root
        self._pathspec = None
        self._simple_patterns = None
        self._git_available = False

        gi_file = root / ".gitignore"
        gi_lines = []
        if gi_file.exists():
            gi_lines = gi_file.read_text(encoding="utf-8", errors="replace").splitlines()

        extra_patterns = extra_patterns or []
        combined_lines = [*gi_lines, *extra_patterns]

        # 1) pathspec (si está instalado)
        self._extra_simple = None  # para fallback en rama git
        try:
            import pathspec  # type: ignore
            self._pathspec = pathspec.PathSpec.from_lines("gitwildmatch", combined_lines)
        except Exception:
            self._pathspec = None

        # 2) git check-ignore (rápido para lotes si git está disponible)
        if (root / ".git").exists():
            try:
                subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                self._git_available = True
            except Exception:
                self._git_available = False

        # 3) matcher simple
        if self._pathspec is None and not self._git_available:
            self._simple_patterns = self._compile_simple_patterns(combined_lines)
        elif self._git_available and extra_patterns:
            # Si dependemos de git para .gitignore, compila un matcher simple
            # solo para los extra_patterns (git no los conoce).
            self._extra_simple = self._compile_simple_patterns(extra_patterns)

    def _compile_simple_patterns(self, lines: Iterable[str]) -> Tuple[Set[str], Set[str], Set[str]]:
        """Compila patrones simples tipo fnmatch.
        Devuelve (files, dirs, negaciones) - negaciones solo se consideran en files.
        """
        file_patterns: Set[str] = set()
        dir_patterns: Set[str] = set()
        negations: Set[str] = set()
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("\\#"):
                line = line[1:]  # unescape
            is_neg = line.startswith("!")
            if is_neg:
                line = line[1:].strip()
                negations.add(line)
            if line.endswith("/"):
                dir_patterns.add(line.rstrip("/"))
            else:
                file_patterns.add(line)
        return file_patterns, dir_patterns, negations

    def _any_match(self, rp: str, patterns: Set[str]) -> bool:
        for pat in patterns:
            if pat.startswith("/"):
                if fnmatch.fnmatch("/" + rp, pat):
                    return True
            else:
                if fnmatch.fnmatch(rp, pat) or any(fnmatch.fnmatch(part, pat) for part in rp.split("/")):
                    return True
        return False

    def _simple_is_ignored(self, relpath: Path, is_dir: bool, patterns: Tuple[Set[str], Set[str], Set[str]]) -> bool:
        file_patterns, dir_patterns, negations = patterns
        rp = relpath.as_posix()
        if is_dir and self._any_match(rp, dir_patterns):
            return True
        matched = self._any_match(rp, file_patterns)
        if matched:
            for neg in negations:
                if fnmatch.fnmatch(rp, neg):
                    return False
            return True
        return False

    def is_ignored(self, relpath: Path, is_dir: bool = False) -> bool:
        rp = relpath.as_posix()

        # pathspec branch (incluye extra patterns porque ya los combinamos)
        if self._pathspec is not None:
            try:
                return self._pathspec.match_file(rp)
            except Exception:
                pass

        # git check-ignore branch -> primero evaluar extra_simple si existe
        if self._git_available:
            if self._extra_simple is not None and self._simple_is_ignored(relpath, is_dir, self._extra_simple):
                return True
            try:
                p = subprocess.run(
                    ["git", "check-ignore", "-q", "--", rp],
                    cwd=self.root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return p.returncode == 0
            except Exception:
                pass

        # simple fnmatch branch (con .gitignore + extras ya combinados)
        if self._simple_patterns is None:
            return False
        return self._simple_is_ignored(relpath, is_dir, self._simple_patterns)

# ------------------------------ detección tech --------------------------------

EXT_LANGUAGE_MAP: Dict[str, str] = {
    # Web / TS / JS
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".json": "JSON",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "SASS",
    ".less": "Less",
    # Common
    ".py": "Python",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".c": "C",
    ".h": "C/C++ Header",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++ Header",
    ".cs": "C#",
    ".go": "Go",
    ".rs": "Rust",
    ".php": "PHP",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".m": "Objective-C",
    ".mm": "Objective-C++",
    ".sh": "Shell",
    ".bat": "Batch",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".md": "Markdown",
    ".txt": "Text",
}

def detect_frameworks(root: Path) -> Tuple[str, Set[str]]:
    """Devuelve (project_name, frameworks_set)."""
    name: Optional[str] = None
    techs: Set[str] = set()

    package_json = root / "package.json"
    angular_json = root / "angular.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                if not name and "name" in data and isinstance(data["name"], str):
                    name = data["name"]
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                keys = set(deps.keys())
                if any(k.startswith("@angular/") for k in keys) or angular_json.exists():
                    techs.add("ANGULAR")
                if "next" in keys:
                    techs.add("NEXTJS")
                if "react" in keys:
                    techs.add("REACT")
                if "vue" in keys or "nuxt" in keys or any(k.startswith("@vue/") for k in keys):
                    techs.add("VUE")
                if "svelte" in keys:
                    techs.add("SVELTE")
                if not {"ANGULAR","REACT","VUE","SVELTE","NEXTJS"}.intersection(techs):
                    techs.add("NODE")
        except Exception:
            pass

    # Python
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        techs.add("PYTHON")
        # intentar leer nombre
        try:
            import tomllib  # Python 3.11+
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
            proj = data.get("project") or {}
            if not name and isinstance(proj, dict) and "name" in proj:
                name = str(proj["name"])
        except Exception:
            # regex simple
            m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', pyproject.read_text(encoding="utf-8", errors="replace"), re.M)
            if m and not name:
                name = m.group(1)

    # Java
    if (root / "pom.xml").exists() or (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        techs.add("JAVA")

    if name is None:
        name = root.name

    return name, techs

# --------------------------- comentarios por tipo -----------------------------

def comment_for_path(relpath: str, suffix: str) -> str:
    s = suffix.lower()
    # Bloque vs línea único
    if s in {".html", ".htm", ".xml"}:
        return f"<!-- {relpath} -->"
    if s in {".css", ".scss", ".sass", ".less"}:
        return f"/* {relpath} */"
    if s in {".md", ".txt"}:
        return f"** {relpath} **"
    if s in {".yml", ".yaml"}:
        return f"# {relpath}"
    if s in {".toml"}:
        return f"# {relpath}"
    if s in {".json"}:
        # JSON no admite comentarios, pero usaremos estilo // para legibilidad
        return f"// {relpath}"
    if s in {".py", ".sh"}:
        return f"# {relpath}"
    # default estilo // para lenguajes C-like (js/ts/java/c#/go/c/cpp/kt/rs/php)
    return f"// {relpath}"

# ------------------------------ utilidades varias -----------------------------

BINARY_SIGNATURES = [
    b"\x00",
]

def is_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(4096)
            if not chunk:
                return False
            for sig in BINARY_SIGNATURES:
                if sig in chunk:
                    return True
            # heuristic: high ratio of non-text bytes
            text_chars = bytearray({7,8,9,10,12,13,27} | set(range(0x20, 0x100)))
            nontext = chunk.translate(None, text_chars)  # type: ignore
            return float(len(nontext)) / max(1, len(chunk)) > 0.30
    except Exception:
        return True

def gather_languages_from_exts(exts: Iterable[str]) -> Set[str]:
    langs = set()
    for e in exts:
        lang = EXT_LANGUAGE_MAP.get(e.lower())
        if lang:
            langs.add(lang)
    return langs

def make_banner(project_name: str, techs: Set[str], langs: Set[str]) -> str:
    title = f"{project_name}".strip()
    tech_label = " / ".join(sorted(techs)) if techs else "Tecnología no detectada"
    lang_label = ", ".join(sorted(langs)) if langs else "Lenguajes no detectados"

    # Un banner llamativo pero simple y compatible con .txt
    line = "/" * (len(title) + 18)
    header = [
        line,
        f"///////****** {title} ******///////",
        line,
        "",
        f"////////  {tech_label}  ////////",
        f"(Lenguajes: {lang_label})",
        "",
        ""
    ]
    return "\n".join(header)

# ------------------------------- programa main --------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Concatena archivos de un proyecto respetando .gitignore.")
    # <--- MODIFICADO: Se añade nargs='?' para hacerlo opcional y default='.' para el valor por defecto.
    parser.add_argument("project_root", nargs='?', default='.', help="Ruta al directorio raíz del proyecto (por defecto: directorio actual).")
    parser.add_argument("--output-dir", help="Directorio donde escribir el .txt (por defecto el raíz del proyecto).")
    parser.add_argument("--extra-ignore", action="append", default=None,
                        help="Patrón adicional a ignorar (sintaxis .gitignore). Puedes repetir la bandera.")
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    if not root.exists() or not root.is_dir():
        print(f"Ruta inválida: {root}", file=sys.stderr)
        return 2

    # combina patrones de archivo con los pasados por CLI
    cli_extras = args.extra_ignore or []
    combined_extras = [*EXTRA_IGNORE_PATTERNS, *cli_extras]

    project_name, techs = detect_frameworks(root)

    out_dir = Path(args.output_dir).resolve() if args.output_dir else root
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{project_name}.txt"

    matcher = IgnoreMatcher(root, extra_patterns=combined_extras)

    collected_exts: Set[str] = set()
    files_to_read: list[Path] = []

    # Evitar carpetas pesadas comunes si no están explícitamente incluidas
    default_skip_dirs = {".git", ".hg", ".svn", "node_modules", "dist", "build", ".next", ".turbo", ".cache", ".venv", "venv", "__pycache__"}

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # filtrar directorios ignorados por .gitignore/extras o default_skip_dirs
        pruned = []
        for d in list(dirnames):
            rel = rel_dir / d
            if d in default_skip_dirs or matcher.is_ignored(rel, is_dir=True):
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)

        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root)
            if matcher.is_ignored(rel, is_dir=False):
                continue
            # saltar archivos ya generados por nosotros
            if full == out_file:
                continue
            # evitar binarios
            if is_binary_file(full):
                continue
            files_to_read.append(full)
            collected_exts.add(full.suffix)

    languages = gather_languages_from_exts(collected_exts)

    # Escribir salida
    with out_file.open("w", encoding="utf-8", errors="replace") as out:
        out.write(make_banner(project_name, techs, languages))

        # Opcional: índice de archivos
        out.write("ÍNDICE DE ARCHIVOS INCLUIDOS:\n")
        for p in files_to_read:
            out.write(f" - {p.relative_to(root).as_posix()}\n")
        out.write("\n" + "="*80 + "\n\n")

        # Contenido de archivos
        for p in files_to_read:
            rel = p.relative_to(root).as_posix()
            comment = comment_for_path(rel, p.suffix)
            out.write(comment + "\n\n")
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                text = f"<<No se pudo leer el archivo por error: {e}>>"
            out.write(text.rstrip() + "\n\n")
            out.write("\n" + "-"*80 + "\n\n")

    print(f"Archivo generado: {out_file}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

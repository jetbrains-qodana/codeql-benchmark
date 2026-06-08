#!/usr/bin/env python3
"""
Build self-contained per-language benchmarks from CodeQL's test files.

Usage:
    python3 build_benchmark.py <language> [--codeql-repo /path/to/codeql] [--clean]

<language> is one of: csharp, javascript, all.

Outputs land under <language>/ at the script's directory. Each language has
its own tests.yaml and qodana.yaml. See README.md for the full layout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

# Languages supported by this builder.
LANGUAGES = ("csharp", "javascript")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def load_tests_config(path: Path) -> list[str]:
    """Tiny YAML subset parser: `tests:` followed by `- entry` lines."""
    tests: list[str] = []
    in_tests = False
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("tests:"):
            in_tests = True
            continue
        if in_tests:
            stripped = line.lstrip()
            if stripped.startswith("- "):
                tests.append(stripped[2:].strip().strip('"').strip("'"))
            elif not line.startswith((" ", "\t")):
                in_tests = False
    if not tests:
        raise SystemExit(f"No tests found in {path}")
    return tests


def relpath_for_csproj(target: Path, csproj_dir: Path) -> str:
    """POSIX-style relative path suitable for csproj Include attributes."""
    return os.path.relpath(target, csproj_dir).replace("\\", "/")


def remove_path(p: Path) -> bool:
    if p.is_dir():
        shutil.rmtree(p)
        return True
    if p.exists():
        p.unlink()
        return True
    return False


# ===========================================================================
# C# builder
# ===========================================================================
CSHARP_TEST_ROOT_REL = Path("csharp/ql/test/query-tests/Security Features")
CSHARP_STUBS_ROOT_REL = Path("csharp/ql/test/resources/stubs")

CSHARP_TEST_FILE_EXTS = {".cs", ".cshtml", ".aspx", ".expected", ".qlref",
                         ".yml", ".yaml", ".ql"}
CSHARP_COMPILE_EXTS = {".cs"}

# Generated paths inside csharp/. --clean only touches these.
CSHARP_GENERATED = ("CodeQLBenchmark.sln", "Benchmark", "Stubs")

# Deterministic namespace for project GUIDs so re-runs produce identical .sln.
GUID_NS = uuid.UUID("e9b6f5d8-0b8e-4c5e-9c9d-2a1f3e4b5c6a")
SDK_PROJECT_TYPE_GUID = "9A19103F-16F7-4668-BE54-9A1E7A4F7556"

PROJECT_RE = re.compile(r"--load-sources-from-project:\S*?stubs/(\S+\.csproj)")
STANDALONE_RE = re.compile(
    r"(?<!project:)\$\{testdir\}/\S*?stubs/([^/\s]+\.cs)\s*$")


def parse_options(options_path: Path) -> tuple[list[str], list[str]]:
    if not options_path.exists():
        return [], []
    text = options_path.read_text()
    return PROJECT_RE.findall(text), STANDALONE_RE.findall(text)


def resolve_csproj_deps(stub_csproj_rel: str, stubs_root: Path,
                        resolved: set[str]) -> None:
    if stub_csproj_rel in resolved:
        return
    resolved.add(stub_csproj_rel)
    csproj_abs = stubs_root / stub_csproj_rel
    if not csproj_abs.exists():
        print(f"WARNING: missing stub csproj: {csproj_abs}", file=sys.stderr)
        return
    try:
        tree = ET.parse(csproj_abs)
    except ET.ParseError as e:
        print(f"WARNING: failed to parse {csproj_abs}: {e}", file=sys.stderr)
        return
    for ref in tree.iter("ProjectReference"):
        include = ref.get("Include")
        if not include:
            continue
        ref_abs = (csproj_abs.parent / include).resolve()
        try:
            ref_rel = ref_abs.relative_to(stubs_root.resolve())
        except ValueError:
            print(f"WARNING: ref escapes stubs/: {ref_abs}", file=sys.stderr)
            continue
        resolve_csproj_deps(str(ref_rel), stubs_root, resolved)


def copy_csharp_test_folder(src: Path, dst: Path) -> int:
    count = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "options":
            continue
        if path.suffix.lower() not in CSHARP_TEST_FILE_EXTS:
            continue
        rel = path.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def copy_stub_csproj(csproj_rel: str, stubs_root: Path, dst_root: Path) -> None:
    src_dir = (stubs_root / csproj_rel).parent
    dst_dir = (dst_root / csproj_rel).parent
    dst_dir.mkdir(parents=True, exist_ok=True)
    for path in src_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, dst_dir / path.name)


def copy_standalone_stub(name: str, stubs_root: Path, dst_root: Path) -> Path:
    dst_dir = dst_root / "_standalone"
    dst_dir.mkdir(parents=True, exist_ok=True)
    src = stubs_root / name
    dst = dst_dir / name
    shutil.copy2(src, dst)
    return dst


BENCHMARK_CSPROJ_TEMPLATE = """<Project Sdk="Microsoft.NET.Sdk">

  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <OutputType>Library</OutputType>
    <NoStdLib>true</NoStdLib>
    <NoConfig>true</NoConfig>
    <DisableImplicitFrameworkReferences>true</DisableImplicitFrameworkReferences>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <EnableDefaultNoneItems>false</EnableDefaultNoneItems>
    <NoWarn>$(NoWarn);CS0436;CS0246;CS8019;CS1701;CS0234;CS1503;CS0535;CS0117;CS0103;CS0029;CS0426;CS0738;CS0411;CS0119;CS0122;CS0518;CS1061;CS0006</NoWarn>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
  </PropertyGroup>

  <ItemGroup>
{project_refs}
  </ItemGroup>

  <ItemGroup>
{compile_items}
  </ItemGroup>

  <ItemGroup>
{none_items}
  </ItemGroup>

</Project>
"""


def write_benchmark_csproj(csproj_path: Path,
                           stub_csprojs_abs: list[Path],
                           compile_files: list[Path],
                           none_files: list[Path]) -> None:
    csproj_dir = csproj_path.parent
    project_refs = "\n".join(
        f'    <ProjectReference Include="{relpath_for_csproj(p, csproj_dir)}" />'
        for p in sorted(stub_csprojs_abs)
    )
    compile_items = "\n".join(
        f'    <Compile Include="{relpath_for_csproj(p, csproj_dir)}" />'
        for p in sorted(compile_files)
    )
    none_items = "\n".join(
        f'    <None Include="{relpath_for_csproj(p, csproj_dir)}" />'
        for p in sorted(none_files)
    )
    csproj_path.write_text(BENCHMARK_CSPROJ_TEMPLATE.format(
        project_refs=project_refs,
        compile_items=compile_items,
        none_items=none_items,
    ))


def project_guid_for(path: Path) -> str:
    rel = str(path).replace("\\", "/")
    return "{" + str(uuid.uuid5(GUID_NS, rel)).upper() + "}"


def write_solution(sln_path: Path, benchmark_csproj: Path,
                   stub_csprojs: list[Path]) -> None:
    sln_dir = sln_path.parent
    projects: list[tuple[str, Path, str]] = [(
        "Benchmark", benchmark_csproj,
        project_guid_for(benchmark_csproj.relative_to(sln_dir)),
    )]
    for csproj in sorted(stub_csprojs):
        projects.append((
            csproj.stem, csproj,
            project_guid_for(csproj.relative_to(sln_dir)),
        ))

    lines = [
        "Microsoft Visual Studio Solution File, Format Version 12.00",
        "# Visual Studio Version 17",
        "VisualStudioVersion = 17.0.31903.59",
        "MinimumVisualStudioVersion = 10.0.40219.1",
    ]
    for name, csproj, guid in projects:
        rel = relpath_for_csproj(csproj, sln_dir).replace("/", "\\")
        lines.append(
            f'Project("{{{SDK_PROJECT_TYPE_GUID}}}") = "{name}", "{rel}", "{guid}"'
        )
        lines.append("EndProject")
    lines.append("Global")
    lines.append("\tGlobalSection(SolutionConfigurationPlatforms) = preSolution")
    lines.append("\t\tDebug|Any CPU = Debug|Any CPU")
    lines.append("\t\tRelease|Any CPU = Release|Any CPU")
    lines.append("\tEndGlobalSection")
    lines.append("\tGlobalSection(ProjectConfigurationPlatforms) = postSolution")
    for _, _, guid in projects:
        for cfg in ("Debug|Any CPU", "Release|Any CPU"):
            lines.append(f"\t\t{guid}.{cfg}.ActiveCfg = {cfg}")
            lines.append(f"\t\t{guid}.{cfg}.Build.0 = {cfg}")
    lines.append("\tEndGlobalSection")
    lines.append("\tGlobalSection(SolutionProperties) = preSolution")
    lines.append("\t\tHideSolutionNode = FALSE")
    lines.append("\tEndGlobalSection")
    lines.append("EndGlobal")
    sln_path.write_text("\n".join(lines) + "\n")


def build_csharp(codeql_root: Path, lang_dir: Path, clean: bool) -> None:
    test_root = codeql_root / CSHARP_TEST_ROOT_REL
    stubs_root = codeql_root / CSHARP_STUBS_ROOT_REL
    if not test_root.exists():
        raise SystemExit(f"CodeQL C# test root not found: {test_root}")
    if not stubs_root.exists():
        raise SystemExit(f"CodeQL C# stubs root not found: {stubs_root}")

    if clean:
        for name in CSHARP_GENERATED:
            if remove_path(lang_dir / name):
                print(f"Removed csharp/{name}")

    lang_dir.mkdir(parents=True, exist_ok=True)
    benchmark_dir = lang_dir / "Benchmark"
    stubs_out = lang_dir / "Stubs"
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    stubs_out.mkdir(parents=True, exist_ok=True)

    config_path = lang_dir / "tests.yaml"
    tests = load_tests_config(config_path)
    print(f"[csharp] Loaded {len(tests)} test folders from {config_path}")

    csproj_refs: set[str] = set()
    standalone_refs: set[str] = set()
    total_files = 0
    for test_rel in tests:
        src = test_root / test_rel
        if not src.exists():
            print(f"WARNING: missing test folder: {src}", file=sys.stderr)
            continue
        dst = benchmark_dir / test_rel
        n = copy_csharp_test_folder(src, dst)
        total_files += n
        print(f"  {test_rel}: {n} files -> Benchmark/{test_rel}/")

        csprojs, standalones = parse_options(src / "options")
        csproj_refs.update(csprojs)
        standalone_refs.update(standalones)

    resolved_csprojs: set[str] = set()
    for c in csproj_refs:
        resolve_csproj_deps(c, stubs_root, resolved_csprojs)
    print(f"[csharp] Resolved {len(resolved_csprojs)} stub project(s) "
          f"(direct: {len(csproj_refs)}, "
          f"transitive: {len(resolved_csprojs) - len(csproj_refs)})")

    for csproj_rel in sorted(resolved_csprojs):
        copy_stub_csproj(csproj_rel, stubs_root, stubs_out)

    standalone_copied: list[Path] = []
    for name in sorted(standalone_refs):
        src = stubs_root / name
        if not src.exists():
            print(f"WARNING: missing standalone stub: {src}", file=sys.stderr)
            continue
        standalone_copied.append(copy_standalone_stub(name, stubs_root, stubs_out))
    print(f"[csharp] Copied {len(standalone_copied)} standalone .cs stub(s)")

    compile_files: list[Path] = []
    none_files: list[Path] = []
    for path in benchmark_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in CSHARP_COMPILE_EXTS:
            compile_files.append(path)
        else:
            none_files.append(path)
    compile_files.extend(standalone_copied)

    benchmark_csproj = benchmark_dir / "Benchmark.csproj"
    stub_csprojs_abs = sorted(stubs_out / p for p in resolved_csprojs)
    write_benchmark_csproj(benchmark_csproj, stub_csprojs_abs,
                           compile_files, none_files)
    print(f"[csharp] Wrote Benchmark/Benchmark.csproj "
          f"({len(compile_files)} compile, {len(none_files)} none, "
          f"{len(stub_csprojs_abs)} stub refs)")

    sln_path = lang_dir / "CodeQLBenchmark.sln"
    write_solution(sln_path, benchmark_csproj, stub_csprojs_abs)
    print(f"[csharp] Wrote CodeQLBenchmark.sln "
          f"({1 + len(stub_csprojs_abs)} projects)")
    print(f"[csharp] Done. Open {sln_path} in Rider.")


# ===========================================================================
# JavaScript/TypeScript builder
# ===========================================================================
JS_TEST_ROOT_REL = Path("javascript/ql/test/query-tests/Security")

JS_TEST_FILE_EXTS = {".js", ".ts", ".tsx", ".jsx", ".vue", ".html",
                     ".expected", ".qlref", ".yml", ".yaml", ".json", ".d.ts"}

# Files inside javascript/ that --clean touches (in addition to any
# CWE-* directories at the language root).
JS_GENERATED_FILES = ("package.json", "tsconfig.json")

JS_TSCONFIG = {
    "compilerOptions": {
        "target": "ES2022",
        "module": "ESNext",
        "moduleResolution": "node",
        "allowJs": True,
        "checkJs": False,
        "noEmit": True,
        "jsx": "preserve",
        "skipLibCheck": True,
        "esModuleInterop": True,
        "resolveJsonModule": True,
        "allowSyntheticDefaultImports": True,
    },
    "include": ["CWE-*/**/*"],
}

JS_PACKAGE_JSON = {
    "name": "codeql-js-benchmark",
    "version": "0.0.0",
    "private": True,
    "description": "CodeQL JavaScript/TypeScript benchmark — generated by build_benchmark.py.",
}


def copy_js_test_folder(src: Path, dst: Path) -> int:
    count = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        # Skip .qodana metadata directories (user's local Qodana caches).
        if any(part.startswith(".qodana") or part.startswith(".claude")
               for part in path.relative_to(src).parts):
            continue
        if path.name == "options":
            continue
        # Allow files with no extension only if they're notable; otherwise filter.
        suffix = path.suffix.lower()
        # Handle .d.ts as a special case (two-dot suffix).
        if path.name.endswith(".d.ts"):
            pass
        elif suffix not in JS_TEST_FILE_EXTS:
            continue
        rel = path.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def build_javascript(codeql_root: Path, lang_dir: Path, clean: bool) -> None:
    test_root = codeql_root / JS_TEST_ROOT_REL
    if not test_root.exists():
        raise SystemExit(f"CodeQL JS test root not found: {test_root}")

    if clean:
        # Remove any CWE-* directories (they were created by previous runs).
        if lang_dir.exists():
            for child in lang_dir.iterdir():
                if child.is_dir() and child.name.startswith("CWE-"):
                    shutil.rmtree(child)
                    print(f"Removed javascript/{child.name}")
        for name in JS_GENERATED_FILES:
            if remove_path(lang_dir / name):
                print(f"Removed javascript/{name}")

    lang_dir.mkdir(parents=True, exist_ok=True)

    config_path = lang_dir / "tests.yaml"
    tests = load_tests_config(config_path)
    print(f"[javascript] Loaded {len(tests)} test folders from {config_path}")

    total_files = 0
    for test_rel in tests:
        src = test_root / test_rel
        if not src.exists():
            print(f"WARNING: missing test folder: {src}", file=sys.stderr)
            continue
        dst = lang_dir / test_rel
        n = copy_js_test_folder(src, dst)
        total_files += n
        print(f"  {test_rel}: {n} files -> {test_rel}/")

    # Write package.json (deterministic — sorted keys, 2-space indent).
    (lang_dir / "package.json").write_text(
        json.dumps(JS_PACKAGE_JSON, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    print(f"[javascript] Wrote package.json")

    # Write tsconfig.json.
    (lang_dir / "tsconfig.json").write_text(
        json.dumps(JS_TSCONFIG, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    print(f"[javascript] Wrote tsconfig.json")

    print(f"[javascript] Done. Open {lang_dir} in WebStorm.")
    print(f"  test source files copied: {total_files}")


# ===========================================================================
# Main
# ===========================================================================
BUILDERS = {
    "csharp": build_csharp,
    "javascript": build_javascript,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build CodeQL per-language benchmarks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "language", choices=list(LANGUAGES) + ["all"],
        help="Which language to build (csharp | javascript | all).",
    )
    parser.add_argument(
        "--codeql-repo", type=Path, required=True,
        help="Path to a local CodeQL checkout "
             "(https://github.com/github/codeql).",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Delete the generated paths for the selected language(s) first. "
             "Never touches source files (build_benchmark.py, tests.yaml, qodana.yaml).",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    codeql_root = args.codeql_repo.resolve()
    if not codeql_root.exists():
        raise SystemExit(f"CodeQL repo not found at {codeql_root}")

    if args.language == "all":
        targets = list(LANGUAGES)
    else:
        targets = [args.language]

    for lang in targets:
        lang_dir = script_dir / lang
        BUILDERS[lang](codeql_root, lang_dir, args.clean)
    return 0


if __name__ == "__main__":
    sys.exit(main())

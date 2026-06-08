# CodeQL Multi-Language Benchmark

Builds self-contained per-language benchmarks from a subset of CodeQL's
security test files (https://github.com/github/codeql). 
Output is a per-language project tree (e.g., 
`csharp/`, `javascript/`) that can be opened in JetBrains IDEs and
analyzed with Qodana.

The generated files are committed to this repo — you can clone and scan
immediately without needing a local CodeQL checkout. Re-run `build_benchmark.py` only
when you want to refresh from a newer CodeQL revision.

## Rebuilding from CodeQL

You'll need a local checkout of <https://github.com/github/codeql>. Pass
its path with `--codeql-repo`:

```bash
python3 build_benchmark.py csharp      --codeql-repo /path/to/codeql --clean
python3 build_benchmark.py javascript  --codeql-repo /path/to/codeql --clean
python3 build_benchmark.py all         --codeql-repo /path/to/codeql --clean
```

### Build flags

| Flag             | Description                                                       |
| ---------------- | ----------------------------------------------------------------- |
| `language`       | `csharp`, `javascript`, or `all` (required)                       |
| `--codeql-repo`  | Path to a local CodeQL checkout (required)                        |
| `--clean`        | Delete the selected language's generated paths before rebuilding  |

`--clean` removes only the generated paths inside the selected language's
subdirectory: for C# that's `csharp/{CodeQLBenchmark.sln,Benchmark,Stubs}`;
for JS that's `javascript/{package.json,tsconfig.json}` plus every
`javascript/CWE-*/` directory. It never touches `build_benchmark.py`,
`tests.yaml`, `qodana.yaml`, `README.md`, or the other language's subtree.

## Running Qodana

### C#

```bash
docker run --rm -it \
  -v "$PWD/csharp":/data/project \
  -v "$PWD/csharp/results":/data/results \
  -p 8080:8080 \
  -e QODANA_TOKEN="$QODANA_TOKEN" \
  jetbrains/qodana-dotnet:2026.1 \
  --show-report
```

### JavaScript / TypeScript

```bash
docker run --rm -it \
  -v "$PWD/javascript":/data/project \
  -v "$PWD/javascript/results":/data/results \
  -p 8080:8080 \
  -e QODANA_TOKEN="$QODANA_TOKEN" \
  jetbrains/qodana-dotnet:2026.1 \
  --show-report
```

## Adding more tests

Edit the relevant `<language>/tests.yaml` and add another entry under
`tests:`. Then `python3 build_benchmark.py <language> --clean` and commit.

## Adding more languages

The script dispatches on the positional `<language>` argument; each
language has its own `build_<language>(codeql_root, lang_dir, clean)`
function. To add Python/Java/Go/etc., add a new builder function and
register it in the `BUILDERS` dict in `build_benchmark.py`.

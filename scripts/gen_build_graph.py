#!/usr/bin/env python3
"""Build dependency graph generator.

Usage:
    make ... VERBOSE=true | python scripts/gen_build_graph.py

Reads build "Needing" log lines from stdin and outputs a DOT file.
"""

import sys
from pathlib import Path

OUTPUT_DOT = Path("build_deps.dot")
OUTPUT_SVG = Path("build_deps.svg")


def extract_dependencies() -> dict[str, list[str]]:
    """Extract dependencies from stdin."""
    rules: dict[str, list[str]] = {}
    current_target: dict[str, str] = {}  # thread name -> target

    for line in sys.stdin:
        # Extract thread name like Builder_0, Builder_1, etc.
        thread_match = line.find("(Builder_")
        if thread_match == -1:
            continue
        thread_end = line.find(")", thread_match)
        if thread_end == -1:
            continue
        thread = line[thread_match + 1 : thread_end]

        # Look for "Building: X"
        if "Building:" in line:
            parts = line.split("Building:")
            if len(parts) > 1:
                target = parts[1].strip()
                current_target[thread] = target
                if target not in rules:
                    rules[target] = []
        # Look for "Needing: Y"
        elif "Needing:" in line:
            parts = line.split("Needing:")
            if len(parts) > 1:
                dep = parts[1].strip()
                parent = current_target.get(thread)
                if parent and dep and dep not in rules[parent]:
                    rules[parent].append(dep)

    return rules


def rules_to_dot(rules: dict[str, list[str]], output: Path) -> None:
    """Write rules to a DOT file without clusters."""
    # Separate dynamic targets
    assets = set()
    pages = set()

    for deps in rules.values():
        for dep in deps:
            if dep.startswith("copy_assets:") or dep == "copy_opml":
                assets.add(dep)
            elif dep.startswith("render_page:"):
                pages.add(dep)

    lines = [
        "digraph build_dependencies {",
        "    rankdir=LR;",
        '    node [shape=box, style=rounded, fontname="Helvetica"];',
        "",
        '    "website";',
        "",
    ]

    # All nodes with colors by category
    all_nodes = set(rules.keys())
    for deps in rules.values():
        all_nodes.update(deps)

    for node in sorted(all_nodes):
        lines.append(f'    "{node}";')

    lines.append("")
    lines.append("    // Dependencies")

    for rule, deps in sorted(rules.items()):
        for dep in sorted(set(deps)):
            if dep in rules or dep in pages or dep in assets:
                lines.append(f'    "{rule}" -> "{dep}";')

    lines.append("}")

    output.write_text("\n".join(lines))


def main() -> int:
    print("Reading build trace from stdin...")
    rules = extract_dependencies()

    if not rules:
        print(
            "No dependencies found. Make sure to pipe: make ... VERBOSE=true | python scripts/gen_build_graph.py"
        )
        return 1

    print(f"Found {len(rules)} build targets")
    for target, deps in sorted(rules.items()):
        if deps:
            print(f"  {target} -> {', '.join(deps)}")

    rules_to_dot(rules, OUTPUT_DOT)
    print(f"Wrote {OUTPUT_DOT}")

    try:
        import subprocess

        subprocess.run(
            ["dot", "-Tsvg", str(OUTPUT_DOT), "-o", str(OUTPUT_SVG)],
            check=True,
        )
        print(f"Generated {OUTPUT_SVG}")
        OUTPUT_DOT.unlink()
        print(f"Removed {OUTPUT_DOT}")
    except FileNotFoundError:
        print("graphviz not installed, skipping SVG generation", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

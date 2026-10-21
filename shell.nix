{
  pkgs ? import <nixpkgs> { },
}:
let
  pythonPackages =
    ps: with ps; [
      requests
      feedparser
      feedgen
      icalendar
      pystache
      python-dateutil
      beautifulsoup4
      markdown
      pygments
      favicon
      pyyaml
      pydantic
      readability-lxml
    ];
  pythonEnv = pkgs.python3.withPackages pythonPackages;
  run = pkgs.writeShellScriptBin "run" ''
    ${pythonEnv}/bin/python generator.py blogroll.opml _site
  '';

  # The prebuilt tinysearch binary (not in nixpkgs), packaged in its own file.
  tinysearchBin = import ./nix/tinysearch.nix { inherit pkgs; };
in
pkgs.mkShell {
  buildInputs = with pkgs; [
    pythonEnv
    black
    ruff
    basedpyright

    # Full-text search (/search/): the prebuilt indexer compiles the browser
    # engine to wasm via cargo; wasm-opt (binaryen) shrinks the artifact.
    # nixpkgs rustc is built --disable-lld, so the wasm target's linker (lld)
    # must come from nixpkgs separately.
    tinysearchBin
    rustc
    cargo
    lld
    # binaryen: nixpkgs' version, which CI pins to the same upstream
    # release so both environments optimise the engine the same way.
    binaryen

    run
  ];

  # Engine-crate crates.io downloads land in the gitignored .cache/cargo so
  # they persist across shell reloads. (CI points CARGO_HOME at .cargo and
  # caches that separately.) The path is resolved from the repository root,
  # so it matches the one the Makefile uses no matter where the shell is
  # started from.
  shellHook = ''
    repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
    export CARGO_HOME="''${CARGO_HOME:-$repo_root/.cache/cargo}"
  '';
}

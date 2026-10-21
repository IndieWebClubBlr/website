# The prebuilt tinysearch binary.
#
# tinysearch is not packaged in nixpkgs, so the release binary is fetched
# and unpacked here. The version is pinned and must be kept in sync with
# .github/workflows/update.yml — bump both together. The sha256s are the
# per-asset digests published with the GitHub release; recompute them when
# bumping the version.
#
# Usage:
#   import ./nix/tinysearch.nix { inherit pkgs; }
{ pkgs ? import <nixpkgs> { } }:

let
  version = "0.11.1";

  assets = {
    "aarch64-darwin" = {
      arch = "aarch64-apple-darwin";
      sha256 = "236dd8c1ee7de8c7d5309b0c96b29aa4a4463638712f81f95963408e79a2b714";
    };
    "x86_64-darwin" = {
      arch = "x86_64-apple-darwin";
      sha256 = "4f03e459599b8a4f0a6ebdad188165152dd4be515951cca93b5effb91c2820a9";
    };
    "x86_64-linux" = {
      arch = "x86_64-unknown-linux-gnu";
      sha256 = "30ab7459c911ca2087aee7c6a6aa698eab806ed528e4fc588543b35fa8bf6a67";
    };
  };

  asset =
    assets.${pkgs.stdenv.hostPlatform.system}
    or (throw "tinysearch: unsupported system ${pkgs.stdenv.hostPlatform.system}");
in
pkgs.stdenv.mkDerivation {
  pname = "tinysearch";
  inherit version;

  src = pkgs.fetchurl {
    url = "https://github.com/tinysearch/tinysearch/releases/download/v${version}/tinysearch-v${version}-${asset.arch}.tar.gz";
    inherit (asset) sha256;
  };

  # The release tarball contains a single top-level binary, not a directory.
  dontUnpack = true;

  installPhase = ''
    runHook preInstall
    tar -xzf "$src"
    install -Dm755 tinysearch $out/bin/tinysearch
    runHook postInstall
  '';
}

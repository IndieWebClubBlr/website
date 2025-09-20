{
  pkgs ? import <nixpkgs> { },
}:
let
  pythonPackages =
    ps: with ps; [
      requests
      feedparser
      pystache
      python-dateutil
    ];
  pythonEnv = pkgs.python3.withPackages pythonPackages;
  run = pkgs.writeShellScriptBin "run" ''
    ${pythonEnv}/bin/python generator.py iwcb.opml _site/index.html
  '';
in
pkgs.mkShell {
  buildInputs = with pkgs; [
    pythonEnv
    black

    run
  ];
}

{
  pkgs ? import <nixpkgs> { },
}:
let
  pythonPackages =
    ps: with ps; [
      requests
      feedparser
      feedgen
      pystache
      python-dateutil
    ];
  pythonEnv = pkgs.python3.withPackages pythonPackages;
  run = pkgs.writeShellScriptBin "run" ''
    ${pythonEnv}/bin/python generator.py iwcb.opml _site/index.html _site/blogroll.atom
  '';
in
pkgs.mkShell {
  buildInputs = with pkgs; [
    pythonEnv
    black

    run
  ];
}

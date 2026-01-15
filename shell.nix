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
    ];
  pythonEnv = pkgs.python3.withPackages pythonPackages;
  run = pkgs.writeShellScriptBin "run" ''
    ${pythonEnv}/bin/python generator.py blogroll.opml _site
  '';
in
pkgs.mkShell {
  buildInputs = with pkgs; [
    pythonEnv
    black
    ruff

    run
  ];
}

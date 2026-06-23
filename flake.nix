{
  description = "Gamasutra postmortem archive";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-darwin"
        "x86_64-linux"
      ];
      forEachSystem = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forEachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312.withPackages (ps: with ps; [
            requests
            tomli-w
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.just
            ];

            shellHook = ''
              echo "mortolista dev shell — try: just"
            '';
          };
        });

      checks = forEachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312.withPackages (ps: with ps; [
            requests
            tomli-w
          ]);
        in
        {
          scraper-imports = pkgs.runCommand "scraper-imports" { } ''
            ${python}/bin/python - <<'PY'
            import py_compile
            import requests
            import tomli_w
            py_compile.compile("${self}/scraper/scrape.py", cfile="/tmp/scrape.pyc", doraise=True)
            PY
            touch $out
          '';
        });
    };
}

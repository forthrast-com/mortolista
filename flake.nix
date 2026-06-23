{
  description = "Gamasutra postmortem archive dev shell";

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
            beautifulsoup4
            lxml
            requests
            tomli-w
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python
              just
            ];

            shellHook = ''
              echo "dev shell: python scraper/scrape.py --sample 20"
            '';
          };
        });
    };
}

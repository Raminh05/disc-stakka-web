{
  description = "Disc Stakka web catalogue";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      # No x86_64-darwin: nixpkgs 26.11 dropped it, and listing it makes
      # `nix flake check --all-systems` fail rather than an Intel Mac work.
      systems = [ "aarch64-darwin" "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # The toolchain only. Python dependencies come from uv.lock, so nix and uv
      # do not each keep their own idea of which Flask this is - the shell used
      # to name flask, pillow, requests and hidapi itself, which drifted from
      # the pinned versions the suite and the image agree on. Ruff is pinned in
      # pyproject's dev group and arrives with `uv sync`, so it is not here
      # either.
      # Prototype: the application as a nix derivation, so it can be built and
      # `nix copy`d to the carousel host and run natively - no Docker, and so
      # none of the device pass-through the container needs.
      packages = forAllSystems (pkgs: {
        default = pkgs.callPackage ./nix/discstakka-web.nix { };
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${pkgs.callPackage ./nix/discstakka-web.nix { }}/bin/discstakka-web";
        };
      });

      nixosModules.default = ./nix/module.nix;

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python314 pkgs.uv ];

          # Use this shell's interpreter rather than one uv downloads: on NixOS a
          # fetched CPython has the wrong dynamic linker and fails obscurely.
          UV_PYTHON = "${pkgs.python314}/bin/python3.14";
          UV_PYTHON_DOWNLOADS = "never";

          shellHook = ''
            echo "uv sync, then uv run app.py   (see AGENTS.md)"
          '';
        };
      });
    };
}

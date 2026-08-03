{
  description = "ledmx - a toolkit for the Framework Laptop 16 LED Matrix input modules";

  # Pinned to the same revision the host system uses, so the dev shell and the
  # package resolve entirely from the existing store instead of pulling a
  # second nixpkgs.
  inputs.nixpkgs.url =
    "github:NixOS/nixpkgs/b5aa0fbd538984f6e3d201be0005b4463d8b09f8";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      pyDeps = ps: with ps; [ pyserial numpy pillow ];
    in
    {
      packages = forAllSystems (pkgs: rec {
        default = ledmx;

        ledmx = pkgs.python3Packages.buildPythonApplication {
          pname = "ledmx";
          version = "0.1.0";
          pyproject = true;
          src = ./.;

          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = pyDeps pkgs.python3Packages;

          # Video decoding shells out to ffmpeg rather than linking a codec
          # library - it handles scaling, cropping and frame rate conversion in
          # one pass, which is exactly the pre-processing the panels need.
          nativeBuildInputs = [ pkgs.makeWrapper ];
          postFixup = ''
            wrapProgram $out/bin/ledmx \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.ffmpeg ]}
          '';

          # No test suite wired into the build yet; the unit tests need no
          # hardware but pytest isn't a dependency of the package itself.
          doCheck = false;

          meta = {
            description = "Animation and video toolkit for Framework 16 LED matrices";
            mainProgram = "ledmx";
            platforms = pkgs.lib.platforms.linux;
          };
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: (pyDeps ps) ++ [ ps.pytest ]))
            pkgs.ffmpeg
            pkgs.inputmodule-control
          ];

          shellHook = ''
            echo "ledmx dev shell - python $(python3 --version | cut -d' ' -f2)"
            echo "  run in-tree with:  python3 -m ledmx --help"
          '';
        };
      });
    };
}

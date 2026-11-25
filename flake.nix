{
  description = "TaiyiDSL Linux hardware worker (no instrument access at build time)";
  inputs.extrapkg.url = "git+https://git.m-labs.hk/M-Labs/artiq-extrapkg.git?ref=release-9";

  outputs = { self, extrapkg }:
    let
      system = "x86_64-linux";
      pkgs = extrapkg.pkgs;
      ps = pkgs.python3Packages;
      # The vendor distributions are pure Python wheels. The native driver is a
      # host prerequisite, never included, imported or exercised by the build.
      lock = (builtins.fromTOML (builtins.readFile ./uv.lock)).package;
      wheel = pname: dependencies:
        let
          p = builtins.head (builtins.filter (dep: dep.name == pname) lock);
          w = builtins.head (builtins.filter (entry: pkgs.lib.hasSuffix "-py3-none-any.whl" entry.url) p.wheels);
        in ps.buildPythonPackage {
          inherit pname dependencies;
          inherit (p) version;
          format = "wheel";
          src = pkgs.fetchurl { inherit (w) url; sha256 = pkgs.lib.removePrefix "sha256:" w.hash; };
          doCheck = false;
        };
      spcm-core = wheel "spcm-core" [];
      spcm = wheel "spcm" [ spcm-core ps.pint ps.numpy ];
      dsl = ps.buildPythonPackage {
        pname = "taiyidsl";
        version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [ ./pyproject.toml ./README.md
            (pkgs.lib.fileset.fileFilter (f: !(f.hasExt "pyc")) ./src/taiyidsl) ];
        };
        pyproject = true;
        build-system = [ ps.hatchling ];
        dependencies = with ps; [ numpy scipy h5py matplotlib plotly xarray ];
        pythonImportsCheck = [ "taiyidsl.runtime.worker" ];
      };
      python = pkgs.python3.withPackages (_: [ extrapkg.packages.${system}.artiq dsl spcm ]);
      launcher = pkgs.writeShellScriptBin "taiyidsl-artiq-python" ''
        unset PYTHONHOME PYTHONPATH SPCM_NO_DLL
        export PYTHONNOUSERSITE=1
        export PATH="${pkgs.lib.makeBinPath [ pkgs.llvmPackages_20.lld pkgs.llvmPackages_20.llvm ]}:$PATH"
        # If needed, point this at a directory containing ONLY the vendor library.
        if [[ -n "''${TAIYIDSL_SPCM_LIB_DIR:-}" ]]; then
          export LD_LIBRARY_PATH="$TAIYIDSL_SPCM_LIB_DIR''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        fi
        exec ${python}/bin/python3 -P "$@"
      '';
    in {
      packages.${system}.hardware-worker = launcher;
      devShells.${system}.hardware = pkgs.mkShell {
        TAIYIDSL_ARTIQ_PYTHON = "${launcher}/bin/taiyidsl-artiq-python";
        packages = [ launcher ];
      };
    };
}

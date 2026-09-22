{
  description = "Theater — cross-harness orchestration layer for coding agents";

  # uv2nix, so the lock file Nix builds from is the same `uv.lock` that `uv run`
  # already uses. The alternative — hand-writing a buildPythonApplication with
  # propagatedBuildInputs — means maintaining the dependency list twice and
  # discovering the drift at build time.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = {
    self,
    nixpkgs,
    flake-utils,
    uv2nix,
    pyproject-nix,
    pyproject-build-systems,
    ...
  }:
    flake-utils.lib.eachDefaultSystem (system: let
      inherit (nixpkgs) lib;

      workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      # Nothing to patch yet: mcp, textual, sqlalchemy and alembic all publish
      # wheels. If a dependency ever fails on a missing build backend, add it
      # here rather than switching the whole workspace to sdist.
      pyprojectOverrides = _final: _prev: {};

      pkgs = import nixpkgs {inherit system;};

      # pyproject.toml asks for >=3.12; 3.12 is what mypy is configured against
      # and what the suite runs on, so pin it rather than following whatever
      # nixpkgs currently calls `python3`.
      python = pkgs.python312;

      pythonSet =
        (pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        })
        .overrideScope
        (lib.composeManyExtensions [
          pyproject-build-systems.overlays.default
          overlay
          pyprojectOverrides
        ]);

      inherit (pkgs.callPackages pyproject-nix.build.util {}) mkApplication;

      # Share matching workspace versions, but expose only each application's CLI.
      applicationEnv = pythonSet.mkVirtualEnv "theater-env" workspace.deps.default;

      theater-unwrapped = mkApplication {
        venv = applicationEnv;
        package = pythonSet.theater;
      };

      regie-unwrapped = mkApplication {
        venv = applicationEnv;
        package = pythonSet.regie;
      };

      # Régie needs tmux; either entry point can launch a daemon that needs git.
      runtimeDeps = [pkgs.tmux pkgs.git];

      wrapApplication = {
        name,
        unwrapped,
        description,
      }:
        pkgs.symlinkJoin {
          inherit name;
          paths = [unwrapped];
          nativeBuildInputs = [pkgs.makeWrapper];
          # Preserve the user's tmux client/server version pairing; Nix is a fallback.
          postBuild = ''
            wrapProgram $out/bin/${name} \
              --suffix PATH : ${lib.makeBinPath runtimeDeps}
          '';
          meta = {
            inherit description;
            mainProgram = name;
          };
        };
    in {
      packages = {
        default = self.packages.${system}.theater;

        theater = wrapApplication {
          name = "theater";
          unwrapped = theater-unwrapped;
          description = "Cross-harness orchestration layer for coding agents";
        };

        regie = wrapApplication {
          name = "regie";
          unwrapped = regie-unwrapped;
          description = "Independent Theater frontend and tmux terminal provider";
        };

        # The same thing without the PATH wrap, for anyone composing their own
        # environment who wants to supply tmux and git themselves.
        inherit theater-unwrapped regie-unwrapped;
      };

      apps = {
        default = self.apps.${system}.theater;
        theater = {
          type = "app";
          program = "${self.packages.${system}.theater}/bin/theater";
        };
        regie = {
          type = "app";
          program = "${self.packages.${system}.regie}/bin/regie";
        };
      };

      checks.cli = pkgs.runCommand "theater-regie-cli-check" {} ''
        ${self.packages.${system}.theater}/bin/theater --version
        ${self.packages.${system}.regie}/bin/regie --help
        test ! -e ${self.packages.${system}.theater}/bin/regie
        test ! -e ${self.packages.${system}.regie}/bin/theater
        test ! -e ${self.packages.${system}.theater}/bin/python
        test ! -e ${self.packages.${system}.regie}/bin/python
        touch "$out"
      '';

      devShells.default = let
        editableOverlay = workspace.mkEditablePyprojectOverlay {
          root = "$REPO_ROOT";
        };

        editablePythonSet = pythonSet.overrideScope (
          lib.composeManyExtensions [
            editableOverlay

            (final: prev: {
              theater = prev.theater.overrideAttrs (old: {
                # Only the files the build itself reads, so editing anything
                # under theater/ does not trigger a rebuild of the venv.
                src = lib.fileset.toSource {
                  root = old.src;
                  fileset = lib.fileset.unions [
                    (old.src + "/pyproject.toml")
                    (old.src + "/README.md")
                  ];
                };

                nativeBuildInputs =
                  old.nativeBuildInputs
                  ++ final.resolveBuildSystem {editables = [];};
              });
            })
          ]
        );

        # deps.all, not deps.default: the dev group carries pytest, ruff and
        # mypy, which are the three gates this project is checked with.
        virtualenv = editablePythonSet.mkVirtualEnv "theater-dev-env" workspace.deps.all;
      in
        pkgs.mkShell {
          # tmux here is not a fallback but a requirement: the suite has tests
          # marked `tmux` that drive a real server, and they silently skip
          # themselves when it is absent.
          packages = [virtualenv pkgs.uv] ++ runtimeDeps;

          env = {
            UV_NO_SYNC = "1";
            UV_PYTHON = "${virtualenv}/bin/python";
            UV_PYTHON_DOWNLOADS = "never";
          };

          shellHook = ''
            unset PYTHONPATH
            export REPO_ROOT=$(git rev-parse --show-toplevel)
          '';
        };
    });
}

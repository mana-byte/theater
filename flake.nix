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

      # The venv holds every dependency; mkApplication exposes only Theater's
      # own `bin/theater`, so installing this does not leak `python`, `alembic`
      # and friends into the user's profile.
      theater-unwrapped = mkApplication {
        venv = pythonSet.mkVirtualEnv "theater-env" workspace.deps.default;
        package = pythonSet.theater;
      };

      # Runtime externals. Theater shells out to bare `tmux` (tmux/client.py)
      # and bare `git` (daemon/worktree.py), neither of which is a Python
      # dependency, so an otherwise complete install still fails at the first
      # `theater ls` without them.
      #
      # `--suffix`, deliberately, not `--prefix`: a tmux client refuses to talk
      # to a server running a different protocol version. If the user already
      # has tmux — which anyone running Theater does, since it lives in their
      # session — theirs must win, or every tmux call Theater makes would fail
      # against the server they started. The tmux below is a fallback for a
      # machine that has none, and in that case it starts the server too, so
      # the pair stays consistent either way.
      runtimeDeps = [pkgs.tmux pkgs.git];
    in {
      packages = {
        default = pkgs.symlinkJoin {
          name = "theater";
          paths = [theater-unwrapped];
          nativeBuildInputs = [pkgs.makeWrapper];
          postBuild = ''
            wrapProgram $out/bin/theater \
              --suffix PATH : ${lib.makeBinPath runtimeDeps}
          '';
          meta = {
            description = "Cross-harness orchestration layer for coding agents";
            mainProgram = "theater";
          };
        };

        # The same thing without the PATH wrap, for anyone composing their own
        # environment who wants to supply tmux and git themselves.
        inherit theater-unwrapped;
      };

      apps.default = {
        type = "app";
        program = "${self.packages.${system}.default}/bin/theater";
      };

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

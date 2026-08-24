{
  description = "LiteLLM proxy: dev shell and runnable package";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));

      litellmVersion = "1.99.0"; # keep in sync with pyproject.toml [project] version

      # Hashes of the wheelhouse per platform. To add a platform (or after
      # uv.lock/pyproject changes), set `nixpkgs.lib.fakeHash` for it, run
      # `nix build .#wheels`, and record the "got: sha256-..." from the error.
      venvHashes = {
        x86_64-linux = "sha256-hp354aH8ywNvv06v2eLr6G3xEvdFWaGc6YmGDPIt5xE=";
      };
      # Prisma derives the platform from /etc/os-release ("linux-nixos"), for which
      # binaries.prisma.sh publishes no engines (404). Pin the glibc build for the
      # commit locked by prisma==0.11.0 in uv.lock (JS CLI 5.4.2), repoint its RPATH
      # at nixpkgs' openssl/libgcc, and export it as PRISMA_QUERY_ENGINE_BINARY so
      # `prisma generate` and the python client skip the download.
      # Bumping prisma in uv.lock changes this commit and hash.
      prismaEngine = pkgs:
        let
          base = "https://binaries.prisma.sh/all_commits/ac9d7041ed77bcc8a8dbd2ab6616b39013829574/debian-openssl-3.0.x";
          queryEngineGz = pkgs.fetchurl {
            url = "${base}/query-engine.gz";
            sha256 = "67f5accb19ac4f5c7ed7bf4cbbaae1131e8b457791d0d72df8bfa2f8b9b23a18";
          };
          schemaEngineGz = pkgs.fetchurl {
            url = "${base}/schema-engine.gz";
            sha256 = "18d348d986b4e6d0682a5af00d8f1f7cd1380f110fe274bb8e773a78ed6cb40a";
          };
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "prisma-engines";
          version = "ac9d7041ed77bcc8a8dbd2ab6616b39013829574";
          src = queryEngineGz;
          nativeBuildInputs = [ pkgs.patchelf ];
          buildInputs = [ pkgs.openssl_3_6.out pkgs.libgcc ];
          dontUnpack = true;
          dontConfigure = true;
          dontBuild = true;
          dontFixup = true; # stdenv's fixup strips the RPATH entries we set
          installPhase = ''
            mkdir -p $out/bin
            gunzip -c $src > $out/bin/query-engine
            gunzip -c ${schemaEngineGz} > $out/bin/schema-engine
            for engine in query-engine schema-engine; do
              chmod +x $out/bin/$engine
              patchelf --set-rpath "${pkgs.openssl_3_6.out}/lib:${pkgs.libgcc}/lib" $out/bin/$engine
            done
          '';
        };
      # Runnable-package plumbing. The litellm runtime env (uv.lock, base +
      # proxy extra) is resolved in three steps:
      #
      # 1. litellmWheels (fixed-output derivation — FODs are the only builds
      #    allowed network): `uv export` the pinned requirements and download
      #    every wheel. Only downloads: a FOD's output must not reference
      #    store paths, so the venv itself cannot be built here. Extras
      #    proxy + proxy-runtime (the Docker image's runtime dependency
      #    set: opentelemetry, langfuse, prometheus-client, sentry, ...).
      #    extra_proxy/semantic-router/saml from the Dockerfile are skipped:
      #    prisma's engine download does not work in a sandbox, and the
      #    other two are niche.
      # 2. litellmVenv (normal derivation): create a venv on nix python312 and
      #    `uv pip install` strictly from the wheelhouse. The litellm package
      #    itself is copied in by hand: installing the root via uv would run
      #    maturin (Rust build) for a pyo3 extension that is optional at
      #    runtime — the loader (litellm/rust_bridge/loader.py) falls back to
      #    pure Python when `_native` is missing. Same for the pure-python
      #    workspace members, whose uv_build backend cannot bootstrap inside a
      #    sandbox (it shells out to an interpreter that is never installed).
      # 3. litellm: `litellm` CLI wrapper.
      litellmWheels = pkgs:
        let
          pip-python = pkgs.python312.withPackages (ps: [ ps.pip ]);
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "litellm-wheels";
          version = litellmVersion;
          src = self;
          outputHashAlgo = "sha256";
          outputHashMode = "recursive";
          outputHash = venvHashes.${pkgs.stdenv.hostPlatform.system} or (throw "litellm-wheels: no hash recorded for ${pkgs.stdenv.hostPlatform.system}; see venvHashes comment in flake.nix");
          dontConfigure = true;
          dontBuild = true;
          nativeBuildInputs = [ pkgs.uv pip-python pkgs.cacert ];
          installPhase = ''
            runHook preInstall
            export HOME=$TMPDIR
            export UV_CACHE_DIR=$TMPDIR/uv-cache
            export UV_PYTHON_DOWNLOADS=never
            export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
            mkdir -p $out/wheels
            # stdout, comments stripped: uv's header echoes the -o path,
            # which would make the FOD reference its own $out
            uv export \
              --frozen \
              --no-dev \
              --extra proxy \
              --extra proxy-runtime \
              --no-emit-workspace \
              --no-hashes \
              -o - | grep -v '^#' > $out/requirements.txt
            ${pip-python}/bin/python -m pip download \
              --no-deps \
              --only-binary :all: \
              --no-cache-dir \
              -r $out/requirements.txt \
              -d $out/wheels
            runHook postInstall
          '';
        };
      litellmVenv = pkgs:
        pkgs.stdenvNoCC.mkDerivation {
          pname = "litellm-venv";
          version = litellmVersion;
          src = self;
          dontConfigure = true;
          dontBuild = true;
          nativeBuildInputs = [ pkgs.uv pkgs.python312 ];
          installPhase = ''
            runHook preInstall
            export HOME=$TMPDIR
            export UV_CACHE_DIR=$TMPDIR/uv-cache
            export UV_PYTHON_DOWNLOADS=never
            uv venv $out/venv --python ${pkgs.python312}/bin/python3
            uv pip install \
              --python $out/venv/bin/python \
              --offline \
              --no-index \
              --find-links ${litellmWheels pkgs}/wheels \
              -r ${litellmWheels pkgs}/requirements.txt
            site=$out/venv/lib/python3.12/site-packages
            # litellm package, as the maturin wheel would ship it: without the
            # proxy/enterprise subtree ([tool.maturin] exclude) and _native.
            # Versions of the dist-info stubs follow pyproject/uv.lock.
            cp -r litellm $site/litellm
            rm -rf $site/litellm/proxy/enterprise
            cp -r enterprise/litellm_enterprise $site/litellm_enterprise
            cp -r litellm-proxy-extras/litellm_proxy_extras $site/litellm_proxy_extras
            mkdir -p $site/litellm-${litellmVersion}.dist-info $site/litellm_enterprise-0.1.59.dist-info $site/litellm_proxy_extras-0.4.89.dist-info
            printf 'Metadata-Version: 2.1\nName: litellm\nVersion: ${litellmVersion}\n' > $site/litellm-${litellmVersion}.dist-info/METADATA
            printf 'Metadata-Version: 2.1\nName: litellm-enterprise\nVersion: 0.1.59\n' > $site/litellm_enterprise-0.1.59.dist-info/METADATA
            printf 'Metadata-Version: 2.1\nName: litellm-proxy-extras\nVersion: 0.4.89\n' > $site/litellm_proxy_extras-0.4.89.dist-info/METADATA
            # local wheel installs embed the store path; drop provenance
            find $site -name direct_url.json -delete
            find $site/litellm $site/litellm_enterprise $site/litellm_proxy_extras -name __pycache__ -type d -prune -exec rm -rf {} +
            # console script for the `litellm` entry point (litellm:run_server)
            cat > $out/venv/bin/litellm <<EOF
#!$out/venv/bin/python
import sys
from litellm import run_server
run_server(sys.argv[1:])
EOF
            chmod +x $out/venv/bin/litellm
            runHook postInstall
          '';
        };
      # `litellm` CLI wrapper. LD_LIBRARY_PATH: prebuilt C-extension wheels
      # (tokenizers, orjson, pydantic-core, ...) need libstdc++/libgcc_s,
      # which nix stdenv does not put on the default library search path.
      litellm = pkgs:
        let
          venv = litellmVenv pkgs;
          gccRuntimeLib = if (pkgs.stdenv.cc ? cc) && (pkgs.stdenv.cc.cc ? lib) then pkgs.stdenv.cc.cc.lib else null;
        in
        pkgs.writeShellScriptBin "litellm" (
          (pkgs.lib.optionalString (gccRuntimeLib != null) ''
            export LD_LIBRARY_PATH="${gccRuntimeLib}/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
          '')
          + ''
            exec "${venv}/venv/bin/litellm" "$@"
          ''
        );
    in
    {
      packages = nixpkgs.lib.genAttrs (builtins.attrNames venvHashes) (system:
        let
          pkgs = import nixpkgs { inherit system; };
          litellm' = litellm pkgs;
        in
        {
          litellm = litellm';
          venv = litellmVenv pkgs;
          wheels = litellmWheels pkgs;
          default = litellm';
        });
      apps = nixpkgs.lib.genAttrs (builtins.attrNames venvHashes) (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/litellm";
        };
      });
      devShells = forAllSystems (pkgs:
        let
          isX8664Linux = pkgs.stdenv.hostPlatform.system == "x86_64-linux";
          engine = if isX8664Linux then prismaEngine pkgs else null;
          # libstdc++.so.6 / libgcc_s.so.1 for the prebuilt C-extension wheels
          # (tokenizers, orjson, pydantic-core, grpcio, ...): nix stdenv does not
          # put the C++ runtime on the default library search path.
          gccRuntimeLib = if (pkgs.stdenv.cc ? cc) && (pkgs.stdenv.cc.cc ? lib) then pkgs.stdenv.cc.cc.lib else null;
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs;
              [
                # Toolchain
                uv # 0.12.5; pyproject [tool.uv].required-version >= 0.10.9
                python312 # 3.12.14; CI test jobs run on 3.12
                # Rust 1.97.1; maturin build backend compiles litellm-rust/crates/python-bridge (rust-version 1.88)
                rust.packages.stable.rustc
                rust.packages.stable.cargo
                rust.packages.stable.clippy
                rust.packages.stable.rustfmt
                nodejs_24 # 24.19.0 (ui/litellm-dashboard .nvmrc)
                # Repo tooling
                gnumake # GNU make 4.4.1; nixpkgs' top-level name, binary is `make`
                git
                perl # Makefile lint-format-changed target
                pkg-config
                stdenv.cc # Rust release builds need a linker on NixOS
                docker # docker-compose.yml: postgres + redis for DB-backed dev
              ]
              ++ pkgs.lib.optionals (engine != null) [ engine ];
            shellHook =
              (pkgs.lib.optionalString (gccRuntimeLib != null) ''
                if [ -n "$LD_LIBRARY_PATH" ]; then
                  export LD_LIBRARY_PATH="${gccRuntimeLib}/lib:$LD_LIBRARY_PATH"
                else
                  export LD_LIBRARY_PATH="${gccRuntimeLib}/lib"
                fi
              '')
              + (pkgs.lib.optionalString (engine != null) ''
                export PRISMA_ENGINES_CHECKSUM_IGNORE_MISSING=1
                export PRISMA_QUERY_ENGINE_BINARY=${engine}/bin/query-engine
                export PRISMA_SCHEMA_ENGINE_BINARY=${engine}/bin/schema-engine
              '');
          };
        });
    };
}

{
  description = "Development environment for the LiteLLM proxy";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));
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
    in
    {
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

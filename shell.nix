{ pkgs ? import <nixpkgs> {} }:

let
  pythonEnv = pkgs.python312.withPackages (ps: with ps; [
    rich tqdm pyyaml requests jinja2
  ]);
in
pkgs.mkShell.override { stdenv = pkgs.gcc13Stdenv; } {
  name = "ffmpeg81-env";
  buildInputs = with pkgs; [
    # Python & базовые сборочные инструменты
    perl
    cargo
    cargo-c
    rustc
    pythonEnv
    cmake ninja meson nasm yasm pkg-config-unwrapped
    autoconf automake libtool m4 gnumake git patchelf chrpath

    # Системные компоненты из components.yaml
    zlib giflib bzip2

    # VAAPI & DRM
    libva libva-utils libdrm

    # Vulkan & Placebo
    vulkan-headers vulkan-loader vulkan-tools

    # OpenCL
    opencl-headers ocl-icd clinfo

    # Intel QSV / oneVPL
    libvpl

    # Wayland для SDL2 (ffplay)
    wayland
    wayland-protocols
    wayland-scanner
    libxkbcommon
  ];

  shellHook = ''
    unset AS
    unset PKG_CONFIG_PATH_FOR_TARGET
    unset NIX_PKG_CONFIG_WRAPPER_TARGET_TARGET_x86_64_unknown_linux_gnu
    export NIX_ENFORCE_NO_NATIVE=0
    export PKG_CONFIG_PATH="$PWD/workspace_81/lib/pkgconfig:$PKG_CONFIG_PATH"
  '';
}

"""Build compatible FFmpeg and FFprobe command-line tools from upstream sources."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from build_portable import ROOT, fetch, sha


FFMPEG = ("https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
          "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e")
ZLIB = ("https://zlib.net/zlib-1.4.tar.gz", "02dd159783677a749c04adbdd02b55b8a2bcc7e054f51b76b549199a916ad584")
MINGW = ("https://github.com/mstorsjo/llvm-mingw/releases/download/20261117/"
         "llvm-mingw-20261117-ucrt-macos-universal.tar.xz",
         "98d340bbb8d46b09ad6886ffba88bd9d5415e131c316de48e6bd7647377446aa")
ZLIB_SOURCES = ("adler32", "crc32", "deflate", "infback", "inffast", "inflate", "inftrees", "trees",
                "zutil", "compress", "uncompr", "gzclose", "gzlib", "gzread", "gzwrite")


def unpack(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as packed:
        packed.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError("源码或工具链包目录结构有误")
    return roots[0]


def run(command, directory, log, *, timeout=900):
    with log.open("ab") as out:
        out.write((repr(command)+"\n").encode())
        try:
            subprocess.run(command,cwd=directory,stdout=out,stderr=subprocess.STDOUT,check=True,timeout=timeout)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
            with log.open("rb") as recent:
                recent.seek(max(0,log.stat().st_size-2000))
                print(recent.read().decode("utf-8","replace"), flush=True)
            raise RuntimeError(f"构建失败，日志 {log}") from exc


def build(target, dist, sources=None):
    if platform.system() != "Darwin":
        raise ValueError("当前构建脚本使用 macOS 主机编译 Mac 版本并交叉编译 Windows x64 版本")
    windows = target=="windows-x86_64"
    if not windows and platform.machine() != "arm64":
        raise ValueError("Apple Silicon 媒体工具须在 arm64 环境构建")
    cache = dist/"cache"
    cache.mkdir(parents=True, exist_ok=True)

    def source(component):
        if sources is None:
            return fetch(*component,cache)
        url, checksum = component
        file = sources/url.rsplit("/",1)[-1]
        if sha(file)!=checksum:
            raise ValueError(f"随包源码摘要不符 {file.name}")
        return file

    ffpkg = source(FFMPEG)
    zpkg = source(ZLIB)
    mingw = fetch(*MINGW,cache) if windows else None
    log = dist/(target+"-ffmpeg-build.log")
    log.unlink(missing_ok=True)
    print(f"构建 {target} FFmpeg · {log}",flush=True)
    with tempfile.TemporaryDirectory(prefix="film-media-",dir="/tmp") as temporary:
        work = Path(temporary)
        ffmpeg = unpack(ffpkg,work/"ffmpeg")
        zlib = unpack(zpkg,work/"zlib")
        toolchain = unpack(mingw,work/"mingw") if mingw else None
        cc, ar = ("clang","ar")
        extra = []
        configure = ["./configure","--disable-autodetect","--disable-doc","--disable-ffplay",
                     "--disable-debug","--disable-network","--disable-gpl","--disable-nonfree",
                     "--disable-version3","--enable-zlib","--enable-small","--disable-x86asm"]
        if toolchain:
            prefix = toolchain/"bin/x86_64-w64-mingw32-"
            cc, ar = str(prefix)+"clang",str(prefix)+"ar"
            extra += ["-static"]
            configure += ["--enable-cross-compile","--target-os=mingw32","--arch=x86_64",
                          f"--cross-prefix={prefix}",f"--cc={cc}"]
        for name in ZLIB_SOURCES:
            run([cc,"-O2","-DHAVE_UNISTD_H","-c",name+".c","-o",name+".o"],zlib,log)
        run([ar,"rcs","libz.a",*(name+".o" for name in ZLIB_SOURCES)],zlib,log)
        configure += [f"--extra-cflags=-I{zlib}", "--extra-ldflags="+" ".join([f"-L{zlib}",*extra])]
        run(configure,ffmpeg,log,timeout=160)
        with (ffmpeg/"ffbuild/config.mak").open("r",encoding="utf-8") as stream:
            enabled = stream.read()
        if "CONFIG_PNG_ENCODER=yes" not in enabled or "CONFIG_AAC_ENCODER=yes" not in enabled:
            raise RuntimeError("构建缺少解码抽帧或 AAC 混音所需组件")
        jobs = min(8,os.cpu_count() or 2)
        suffix = ".exe" if windows else ""
        run(["make",f"-j{jobs}",f"ffmpeg{suffix}",f"ffprobe{suffix}"],ffmpeg,log)
        destination = dist/"media-tools"/target
        destination.mkdir(parents=True,exist_ok=True)
        artifacts = {}
        for name in ("ffmpeg","ffprobe"):
            path = destination/(name+suffix)
            shutil.copy2(ffmpeg/(name+suffix),path)
            artifacts[path.name] = sha(path)
        if not windows:
            for name in artifacts:
                version = subprocess.check_output([str(destination/name),"-version"],text=True)
                if "version 9.0.2 " not in version or "--enable-nonfree" in version or "--enable-gpl" in version:
                    raise RuntimeError("媒体工具版本或编译选项校验失败")
        offers = destination/"source"
        offers.mkdir(exist_ok=True)
        shutil.copy2(ffpkg,offers/ffpkg.name)
        shutil.copy2(zpkg,offers/zpkg.name)
        shutil.copy2(__file__,offers/"build_media_tools.py")
        shutil.copy2(Path(__file__).with_name("build_portable.py"),offers/"build_portable.py")
        shutil.copy2(ffmpeg/"COPYING.LGPLv2.1",offers/"COPYING.LGPLv2.1")
        shutil.copy2(zlib/"LICENSE",offers/"LICENSE.zlib")
        receipt = [{"name":"FFmpeg and FFprobe","version":"9.0.2","license":"LGPL-2.1-or-later",
                    "source":FFMPEG[0],"source_sha256":FFMPEG[1],"binaries":artifacts},
                   {"name":"zlib","version":"1.4","license":"Zlib","source":ZLIB[0],"source_sha256":ZLIB[1]}]
        (destination/"components.json").write_text(json.dumps(receipt,indent=2),encoding="utf-8")
        print(destination,flush=True)


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target",choices=("macos-arm64","windows-x86_64"),required=True)
    parser.add_argument("--output",type=Path,default=ROOT/"dist")
    parser.add_argument("--sources",type=Path,help="从已随包提供的源码归档构建")
    args = parser.parse_args()
    build(args.target,args.output.resolve(),args.sources)

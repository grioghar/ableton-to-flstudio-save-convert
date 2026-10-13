"""Command-line interface: ``als2flp input.als [-o output.flp]``."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import List, Optional

from . import __version__
from .als_reader import AlsReader, iter_sample_paths
from .convert import Options, convert_project


def _parse_path_map(values: Optional[List[str]]):
    out = []
    for v in values or []:
        if "=" not in v:
            raise SystemExit(f"--path-map expects OLD=NEW, got: {v}")
        old, new = v.split("=", 1)
        out.append((old, new))
    return out


def _windows_path(p: str) -> str:
    return p.replace("/", "\\")


def _copy_samples(project, dest_dir: str, log) -> dict:
    """Copy every referenced sample into dest_dir; return {old_path: new_path}."""
    os.makedirs(dest_dir, exist_ok=True)
    mapping = {}
    used = set()
    for src in iter_sample_paths(project):
        if src in mapping:
            continue
        if not os.path.isfile(src):
            continue
        base = os.path.basename(src)
        name, ext = os.path.splitext(base)
        candidate = base
        n = 1
        while candidate.lower() in used:
            n += 1
            candidate = f"{name} ({n}){ext}"
        used.add(candidate.lower())
        dst = os.path.join(dest_dir, candidate)
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copy2(src, dst)
        mapping[src] = os.path.abspath(dst)
    log(f"Copied {len(mapping)} sample file(s) to {dest_dir}")
    return mapping


def convert_file(input_path: str, output_path: str, args, log=print) -> int:
    reader = AlsReader(input_path)
    project = reader.read()
    project.warnings = list(reader.warnings)  # type: ignore[attr-defined]

    if args.title:
        project.title = args.title

    path_map = _parse_path_map(args.path_map)
    transform = None
    if args.copy_samples:
        dest = args.copy_samples
        if dest == "auto":
            dest = os.path.splitext(output_path)[0] + "_Samples"
        mapping = _copy_samples(project, dest, log)
        path_map = [(k, v) for k, v in mapping.items()] + path_map
    if args.windows_paths:
        transform = _windows_path

    options = Options(
        ppq=args.ppq,
        include_session_clips=not args.no_session,
        path_map=path_map,
        path_transform=transform,
        author=args.author or "",
    )
    fl, report = convert_project(project, options)
    data = fl.to_bytes()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "wb") as fh:
        fh.write(data)

    log(f"Wrote {output_path} ({len(data):,} bytes)")
    log(f"  tempo {project.tempo:g} BPM, {project.numerator}/{project.denominator}, "
        f"{report.tracks} playlist tracks, {report.inserts} mixer inserts, "
        f"{report.channels} channels, {report.patterns} patterns, {report.playlist_items} playlist items")
    if report.dropped_notes:
        log(f"  dropped {report.dropped_notes} note(s) that hit no Drum Rack pad")
    if args.verbose:
        for n in report.notes:
            log(f"  note: {n}")
    for w in report.warnings:
        log(f"  warning: {w}")
    if report.missing_samples:
        log(f"  {len(report.missing_samples)} referenced sample file(s) were not found on this machine"
            + ("" if args.verbose else " (use -v to list them)"))
        if args.verbose:
            for m in report.missing_samples:
                log(f"    missing: {m}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="als2flp",
        description="Convert Ableton Live Sets (.als) to native FL Studio projects (.flp).",
    )
    ap.add_argument("inputs", nargs="+", help=".als file(s) to convert")
    ap.add_argument("-o", "--output", help="output .flp path (or a directory when converting several files)")
    ap.add_argument("--ppq", type=int, default=96, help="FL Studio timebase in ticks per quarter note (default 96)")
    ap.add_argument("--no-session", action="store_true", help="ignore Session View clips (only convert the Arrangement)")
    ap.add_argument("--path-map", action="append", metavar="OLD=NEW",
                    help="rewrite sample paths starting with OLD to start with NEW (repeatable)")
    ap.add_argument("--windows-paths", action="store_true", help="write sample paths with backslashes")
    ap.add_argument("--copy-samples", nargs="?", const="auto", metavar="DIR",
                    help="copy all referenced samples into DIR (default: <output>_Samples) and point the project at the copies")
    ap.add_argument("--title", help="project title (default: file name)")
    ap.add_argument("--author", help="project author")
    ap.add_argument("-v", "--verbose", action="store_true", help="print per-track conversion notes and missing files")
    ap.add_argument("--version", action="version", version=f"als2flp {__version__}")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    inputs = args.inputs
    if len(inputs) > 1 and args.output and not os.path.isdir(args.output):
        os.makedirs(args.output, exist_ok=True)
    rc = 0
    for inp in inputs:
        if not os.path.isfile(inp):
            print(f"error: not a file: {inp}", file=sys.stderr)
            rc = 1
            continue
        if args.output and (len(inputs) == 1 and not os.path.isdir(args.output)):
            out = args.output
        else:
            base = os.path.splitext(os.path.basename(inp))[0] + ".flp"
            out = os.path.join(args.output, base) if args.output else os.path.join(os.path.dirname(os.path.abspath(inp)), base)
        try:
            rc |= convert_file(inp, out, args)
        except Exception as exc:  # noqa: BLE001
            print(f"error converting {inp}: {exc}", file=sys.stderr)
            if args.verbose:
                raise
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

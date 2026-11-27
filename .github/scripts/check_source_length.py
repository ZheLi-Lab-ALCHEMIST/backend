"""Check tracked source against the repository's shrinking line-count manifest."""

import argparse
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


LIMIT = 1_000
MANIFEST = ".source-length-baseline.json"
SOURCE_SUFFIXES = {
    ".bat", ".c", ".cc", ".cjs", ".cmd", ".cpp", ".css", ".cts", ".cxx",
    ".frag", ".glsl", ".h", ".hpp", ".html", ".js", ".jsx", ".less", ".mjs",
    ".mts", ".ps1", ".py", ".pyi", ".sass", ".scss", ".sh", ".ts", ".tsx",
    ".vert", ".vue", ".wgsl",
}
PATCH_SUFFIXES = {".patch", ".diff"}


def git(*args, data=None):
    return subprocess.check_output(["git", *args], input=data)


def named_source(path):
    name = PurePosixPath(path)
    if name.suffix.lower() in PATCH_SUFFIXES:
        return name.parts[0] not in {"archive", "work"} or "evidence" not in name.parts
    return (name.suffix.lower() in SOURCE_SUFFIXES or name.name in {"Makefile", "Dockerfile"}
            or path.startswith(".husky/") and not name.suffix)


def scan(revision=None, paths=()):
    blobs = {}
    if revision:
        scope = ("--", *sorted(paths), "assets/ketcher/native-build.json") if paths else ()
        entries = git("--literal-pathspecs", "ls-tree", "-rz", revision, *scope)
    else:
        entries = git("ls-files", "--stage", "-z")
    entries = entries.split(b"\0")[:-1]
    # Workbench checks this generated distribution against its native build manifest.
    shipped_ketcher = any(entry.startswith((b"100644 ", b"100755 ")) and
                          entry.endswith(b"\tassets/ketcher/native-build.json") for entry in entries)
    for entry in entries:
        header, raw_path = entry.split(b"\t", 1)
        mode, second, third = header.split()
        if mode not in (b"100644", b"100755"):
            continue
        path = os.fsdecode(raw_path)
        if shipped_ketcher and path.startswith("assets/ketcher/"):
            continue
        source = named_source(path)
        if not source and PurePosixPath(path).suffix:
            continue
        if revision is None and third != b"0":
            raise ValueError(f"Resolve unmerged source {path!r} before checking")
        blobs[path] = (third if revision else second, source)

    objects = sorted({oid for oid, _ in blobs.values()})
    stream = BytesIO(git("cat-file", "--batch", data=b"".join(oid + b"\n" for oid in objects)))
    facts = {}
    for oid in objects:
        object_id, kind, size = stream.readline().split()
        if object_id != oid or kind != b"blob":
            raise ValueError(f"Expected source blob {oid.decode()}")
        content = stream.read(int(size))
        if stream.read(1) != b"\n":
            raise ValueError(f"Incomplete Git blob {oid.decode()}")
        count = content.count(b"\n") + int(bool(content) and not content.endswith(b"\n"))
        facts[oid] = (b"\0" not in content, content.startswith(b"#!"), count)

    counts = {}
    for path, (oid, source) in blobs.items():
        text, shebang, count = facts[oid]
        if text and (source or shebang):
            counts[path] = count
    return counts


def oversized(counts):
    return {path: count for path, count in counts.items() if count > LIMIT}


def load_manifest(raw):
    def read_entries(pairs):
        result = {}
        for path, count in pairs:
            if path in result or not path or type(count) is not int or count <= LIMIT:
                raise ValueError(f"Invalid or duplicate manifest row for {path!r}")
            result[path] = count
        return result

    entries = json.loads(raw, object_pairs_hook=read_entries)
    if not isinstance(entries, dict):
        raise ValueError(f"{MANIFEST} must contain a JSON object of paths and current line counts")
    return entries


def baseline_limits(seed, baseline, release_push):
    tip = "HEAD" if release_push else baseline
    parents = git("rev-list", "--first-parent", tip).decode().splitlines()
    if baseline not in parents:
        raise ValueError(f"Push predecessor {baseline} must be on the release first-parent history")
    baseline_position = parents.index(baseline)
    if seed not in parents[baseline_position:]:
        raise ValueError(f"Release comparison must contain approved seed {seed} on its first-parent history")
    pushed = set(parents[:baseline_position])
    limits = oversized(scan(seed))
    seed_paths = tuple(limits)
    # A source-only commit can make a previously proposed lower value valid.
    revisions = git("--literal-pathspecs", "log", "--first-parent", "--full-history", "--reverse", "--format=%H",
                    f"{seed}..{parents[0]}", "--", MANIFEST, *seed_paths).decode().splitlines()
    for revision in revisions:
        reductions = {}
        if git("ls-tree", revision, "--", MANIFEST):
            try:
                recorded = load_manifest(git("show", f"{revision}:{MANIFEST}"))
            except ValueError as error:
                sys.stderr.write(f"Ignoring invalid manifest at {revision}: {error}\n")
            else:
                reductions = {path: recorded.get(path, LIMIT) for path, count in limits.items()
                              if recorded.get(path, LIMIT) < count}
        scope = seed_paths if revision in pushed else reductions
        if not scope:
            continue
        counts = scan(revision, scope)
        if revision in pushed:
            for path in seed_paths:
                previous = limits.get(path, LIMIT)
                if counts.get(path, 0) > previous:
                    raise ValueError(f"{revision[:12]}: {path!r}: {counts[path]} lines exceed the release limit of {previous}")
        for path, lowered in reductions.items():
            # A lower allowance is effective only once it describes the actual source.
            actual = max(counts.get(path, 0), LIMIT)
            if actual == lowered:
                if lowered == LIMIT:
                    del limits[path]
                else:
                    limits[path] = lowered
    return limits


def check(actual, recorded, limits):
    errors = []
    for path in sorted(actual.keys() | recorded.keys()):
        count = actual.get(path)
        if count is None:
            errors.append(f"{path!r}: remove retired entry from {MANIFEST}")
            continue
        previous = limits.get(path, LIMIT)
        if count > previous:
            errors.append(f"{path!r}: {count} lines exceed the release limit of {previous}")
        elif recorded.get(path) != count:
            errors.append(f"{path!r}: {MANIFEST} must record exactly {count} lines")
    for error in errors:
        sys.stderr.write(f"{error}\n")
    return bool(errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Write the manifest from staged Git blobs")
    mode.add_argument("--baseline", help="Release comparison commit or ref")
    parser.add_argument("--seed", help="Full SHA of the approved pre-guard commit")
    parser.add_argument("--release-push", action="store_true", help="Check each new release first-parent commit through HEAD")
    args = parser.parse_args()
    os.chdir(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n")))
    counts = scan()
    actual = oversized(counts)
    if args.write:
        Path(MANIFEST).write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        if not args.seed or not re.fullmatch(r"[0-9a-f]{40}", args.seed):
            parser.error("--baseline requires a full --seed SHA")
        recorded = load_manifest(git("show", f":{MANIFEST}"))
        baseline = git("rev-parse", "--verify", "--end-of-options", f"{args.baseline}^{{commit}}").decode().strip()
        if check(actual, recorded, baseline_limits(args.seed, baseline, args.release_push)):
            return 1
    sys.stdout.write(f"Source length: {len(counts)} tracked files, {len(actual)} above {LIMIT:,} lines\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"Source length check failed: {exc}\n")
        raise SystemExit(1) from None

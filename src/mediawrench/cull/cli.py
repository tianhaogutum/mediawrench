"""A local, review-first CLI. stdout is JSON; diagnostics and prompts use stderr."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import simplejson as json

from . import SUBCOMMANDS, __version__
from .merge import MergePlan
from .verify import verify_bundle
from .xmp import FIELDS, SIDES, MergeError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="mediawrench cull",
        description="Merge XMP culling fields while preserving a chosen sidecar's other data.",
        epilog="No images or application databases are modified. An output directory must be new.",
    )
    root.add_argument("--version", action="version", version=f"mediawrench cull {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("plan", "merge"):
        sub = commands.add_parser(name, help=SUBCOMMANDS[name])
        sub.add_argument("base", type=Path, help="last common ancestor XMP")
        sub.add_argument("left", type=Path, help="first computer's XMP")
        sub.add_argument("right", type=Path, help="second computer's XMP")
        sub.add_argument(
            "--history-source",
            required=True,
            choices=SIDES,
            help="retain every non-culling property from this input (history is never merged)",
        )
        if name == "plan":
            sub.add_argument(
                "--format",
                choices=("json", "text"),
                default="json",
                help="JSON decision file (default) or a compact human-readable review",
            )
        if name == "merge":
            sub.epilog = "Fields: " + ", ".join(field.name for field in FIELDS)
            sub.add_argument("--output-dir", required=True, type=Path)
            sub.add_argument(
                "--choose",
                action="append",
                default=[],
                metavar="FIELD=SIDE",
                help="resolve one conflicting field; repeat for each conflict",
            )
            sub.add_argument(
                "--decisions",
                type=Path,
                help="plan JSON with choices filled in; input hashes are checked",
            )
            sub.add_argument(
                "--interactive",
                action="store_true",
                help="prompt separately for every unresolved conflict (requires a terminal)",
            )
    verify = commands.add_parser("verify", help=SUBCOMMANDS["verify"])
    verify.add_argument("directory", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "verify":
            print(json.dumps(verify_bundle(arguments.directory), indent=2))
            return 0
        paths = {side: getattr(arguments, side) for side in SIDES}
        plan = MergePlan.create(paths, arguments.history_source)
        if arguments.command == "plan":
            if arguments.format == "text":
                print(text_plan(plan))
            else:
                print(json.dumps(plan.document(), indent=2, ensure_ascii=True))
            return 0
        choices = plan.load_decisions(arguments.decisions) if arguments.decisions else {}
        for raw in arguments.choose:
            if raw.count("=") != 1:
                raise MergeError(f"invalid --choose {raw!r}; use FIELD=base|left|right")
            field, side = raw.split("=", 1)
            if field in choices:
                raise MergeError(f"duplicate choice for {field}")
            choices[field] = side
        plan.validate_choices(choices, require_all=False)
        if arguments.interactive:
            if not sys.stdin.isatty():
                raise MergeError("--interactive requires a terminal; use --choose or --decisions")
            for field in FIELDS:
                if field.name not in plan.conflicts or field.name in choices:
                    continue
                values = plan.fields[field.name]
                print(f"\nConflict: {field.display}", file=sys.stderr)
                for side in SIDES:
                    print(
                        f"  {side}: {json.dumps(values[side], ensure_ascii=True)}", file=sys.stderr
                    )
                while True:
                    print(
                        "Choose base / left / right (or quit): ",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
                    answer = sys.stdin.readline().strip()
                    if not answer or answer == "quit":
                        raise MergeError("review cancelled; no output was written")
                    if answer in SIDES:
                        choices[field.name] = answer
                        break
                    print("Enter base, left, right, or quit.", file=sys.stderr)
        audit = plan.write_bundle(arguments.output_dir, choices)
        for warning in audit["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        print(
            json.dumps(
                {"output_dir": str(arguments.output_dir.resolve()), "result": audit["result"]},
                indent=2,
            )
        )
        return 0
    except (MergeError, OSError) as exc:
        print(f"mediawrench cull: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "mediawrench cull: cancelled; inspect the output directory if writing had begun",
            file=sys.stderr,
        )
        return 130


def text_plan(plan: MergePlan) -> str:
    document = plan.document()
    lines = [
        f"Keep non-culling XML from: {plan.history_source}",
        f"Other metadata differs: {'yes' if document['other_metadata_differ'] else 'no'}",
        (
            "Missing rating uses darktable's import default; compared separately from zero."
            if plan.rating_absence_policy == "darktable_import_default"
            else "Missing rating means 0; other missing properties are shown as null."
        ),
        "",
    ]
    for name, field in plan.fields.items():
        values = "  ".join(f"{side}={json.dumps(field[side])}" for side in SIDES)
        status = "CONFLICT" if field["status"] == "conflict" else field["status"]
        lines.append(f"{name} [{status}]: {values}")
    lines.append("")
    lines.append("Choices required: " + (", ".join(plan.conflicts) or "none"))
    lines.extend(f"Warning: {warning}" for warning in document["warnings"])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

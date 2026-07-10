"""Inspect Weixin.dll native call sites around known RVAs.

This is a read-only helper for the project-owned WeChat native bridge work. It
does not attach to WeChat or patch any process; it only disassembles a local DLL
copy and reports direct ``call rel32`` references to target RVAs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import capstone
import pefile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DLL = ROOT / "$out" / "Weixin-4.1.10.53.dll"


@dataclass(frozen=True)
class SectionView:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int

    @property
    def end_rva(self) -> int:
        return self.virtual_address + max(self.virtual_size, self.raw_size)


class PEImage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pe = pefile.PE(str(path), fast_load=True)
        self.data = path.read_bytes()
        self.sections = [
            SectionView(
                name=section.Name.rstrip(b"\0").decode("ascii", errors="replace"),
                virtual_address=int(section.VirtualAddress),
                virtual_size=int(section.Misc_VirtualSize),
                raw_offset=int(section.PointerToRawData),
                raw_size=int(section.SizeOfRawData),
            )
            for section in self.pe.sections
        ]

    def section_for_rva(self, rva: int) -> SectionView:
        for section in self.sections:
            if section.virtual_address <= rva < section.end_rva:
                return section
        raise ValueError(f"RVA 0x{rva:x} is outside all sections")

    def rva_to_offset(self, rva: int) -> int:
        section = self.section_for_rva(rva)
        return section.raw_offset + (rva - section.virtual_address)

    def bytes_at(self, rva: int, size: int) -> bytes:
        offset = self.rva_to_offset(rva)
        return self.data[offset : offset + size]


def parse_int(value: str) -> int:
    text = value.strip().lower()
    return int(text, 16) if text.startswith("0x") else int(text)


def code_section(image: PEImage) -> SectionView:
    for section in image.sections:
        if section.name == ".text":
            return section
    for section in image.sections:
        if section.name.startswith(".text"):
            return section
    raise ValueError("No .text section found")


def find_direct_call_xrefs(image: PEImage, target_rva: int) -> list[int]:
    section = code_section(image)
    raw = image.data[section.raw_offset : section.raw_offset + section.raw_size]
    xrefs: list[int] = []
    for index in range(0, max(0, len(raw) - 5)):
        if raw[index] != 0xE8:
            continue
        rel = int.from_bytes(raw[index + 1 : index + 5], "little", signed=True)
        call_rva = section.virtual_address + index
        destination = (call_rva + 5 + rel) & 0xFFFFFFFFFFFFFFFF
        if destination == target_rva:
            xrefs.append(call_rva)
    return xrefs


def disassemble_window(image: PEImage, center_rva: int, *, before: int, after: int) -> list[str]:
    start_rva = max(code_section(image).virtual_address, center_rva - before)
    size = before + after
    code = image.bytes_at(start_rva, size)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = False
    lines: list[str] = []
    for insn in md.disasm(code, start_rva):
        marker = "=>" if insn.address == center_rva else "  "
        lines.append(f"{marker} 0x{insn.address:08x}: {insn.mnemonic:<8} {insn.op_str}")
    return lines


def analyze_target(image: PEImage, target_rva: int, *, before: int, after: int) -> str:
    lines = [f"## target 0x{target_rva:08x}"]
    try:
        section = image.section_for_rva(target_rva)
        lines.append(f"- section: {section.name}")
    except ValueError as exc:
        lines.append(f"- section: {exc}")
        return "\n".join(lines)

    xrefs = find_direct_call_xrefs(image, target_rva)
    lines.append(f"- direct call xrefs: {len(xrefs)}")
    for xref in xrefs:
        lines.append(f"\n### xref 0x{xref:08x}")
        lines.extend(disassemble_window(image, xref, before=before, after=after))
    if not xrefs:
        lines.append("\n(no direct rel32 call xrefs found)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Target RVA to inspect, e.g. 0x5236820. Can be repeated.",
    )
    parser.add_argument("--before", type=lambda value: parse_int(value), default=0x80)
    parser.add_argument("--after", type=lambda value: parse_int(value), default=0x80)
    parser.add_argument(
        "--around",
        action="append",
        default=[],
        help="Disassemble a window around this RVA without xref lookup. Can be repeated.",
    )
    args = parser.parse_args()

    if args.target:
        targets = [parse_int(value) for value in args.target]
    elif args.around:
        targets = []
    else:
        targets = [
            0x52302C0,
            0x5236820,
            0x527CF70,
            0x5281990,
        ]
    image = PEImage(args.dll)
    print(f"# {args.dll}")
    for target in targets:
        print()
        print(analyze_target(image, target, before=args.before, after=args.after))
    for value in args.around:
        around = parse_int(value)
        print()
        print(f"## around 0x{around:08x}")
        for line in disassemble_window(image, around, before=args.before, after=args.after):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

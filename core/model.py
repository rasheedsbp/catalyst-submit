"""Journal-neutral intermediate representation of a manuscript.

Everything upstream (docx/tex/pdf parsing) produces a Manuscript.
Everything downstream (LaTeX/Word rendering) consumes one.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional
import json


@dataclass
class Author:
    name: str
    affiliation: str = ""
    email: str = ""
    orcid: str = ""
    corresponding: bool = False


@dataclass
class Figure:
    label: str            # e.g. "fig1"
    caption: str
    path: str = ""        # extracted image file on disk
    width_frac: float = 0.9


@dataclass
class Table:
    label: str
    caption: str
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Equation:
    label: str
    latex: str
    inline: bool = False
    omml: str = ""      # original Word XML, re-injected so .docx stays editable
    number: int = 0
    image_path: str = ""   # set when a pasted-picture equation is reclassified


@dataclass
class Block:
    """A paragraph-level chunk inside a section."""
    kind: str             # para | figure | table | equation | list
    text: str = ""
    ref: str = ""         # label of figure/table/equation
    # inline equations referenced by ⟪MATHn⟫ placeholders in `text`;
    # each renderer substitutes its own math syntax
    inline_eqs: list = field(default_factory=list)


@dataclass
class Section:
    title: str
    level: int = 1
    blocks: list[Block] = field(default_factory=list)
    children: list["Section"] = field(default_factory=list)


@dataclass
class Reference:
    raw: str
    key: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    title: str = ""
    container: str = ""   # journal / conference / publisher
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""


@dataclass
class Manuscript:
    title: str = ""
    authors: list[Author] = field(default_factory=list)
    abstract: str = ""
    structured_abstract: dict[str, str] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    equations: list[Equation] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    funding: str = ""
    conflicts: str = ""
    data_availability: str = ""

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent)

    def warnings(self) -> list[str]:
        w = []
        if not self.title:
            w.append("No title detected.")
        if not self.authors:
            w.append("No authors detected.")
        if not self.abstract and not self.structured_abstract:
            w.append("No abstract detected.")
        if not self.keywords:
            w.append("No keywords detected.")
        if not self.references:
            w.append("No reference list detected.")
        if not self.sections:
            w.append("No body sections detected — check heading styles in the source.")
        return w

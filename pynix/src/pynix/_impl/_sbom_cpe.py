"""CPEs that a dictionary of NVD product names suggests, for packages that state none.

nixpkgs builds a CPE from ``meta.identifiers.cpeParts`` once a package names
its vendor. Most packages do not. The dictionary is a CSV of ``vendor`` and
``product`` columns, one row for each pair in the NVD CPE dictionary, as
https://github.com/tiiuae/cpedict publishes it. A product that only one
vendor uses gives that vendor; a product with several gives nothing.

A lookup by product name alone does not know the ecosystem: on sg1 the
Python package ``idna`` matched the Rust crate, and ``mcp`` matched an
unrelated project; Perl's CGI matched Ruby's, and a PHP extension has
PHP's version. So a module of a language gets no generated CPE, nor does
a package with no evaluated value, and the document lists a generated
CPE as evidence, never as the component's CPE.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from pynix._impl._sbom_model import Inventory, Origin, Record

#: What a CPE 2.3 formatted string holds unescaped; anything else takes a backslash.
_UNQUOTED = re.compile(r"[A-Za-z0-9_.\-]")


def bind(value: str) -> str:
    """*value* as one component of a CPE 2.3 formatted string."""
    return "".join(c if _UNQUOTED.fullmatch(c) else "\\" + c for c in value)


@dataclass(frozen=True, slots=True)
class CpeDictionary:
    """The one vendor of each product name, both in formatted-string form."""

    vendors: dict[str, str] = field(default_factory=dict[str, str])

    @classmethod
    def parse(cls, text: str) -> CpeDictionary:
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows and not {"vendor", "product"} <= rows[0].keys():
            raise ValueError("a CPE dictionary needs the columns vendor and product")
        pairs = {(row["vendor"], row["product"]) for row in rows}
        uses = Counter(product for _, product in pairs)
        return cls({product: vendor for vendor, product in pairs if uses[product] == 1})


def generated_cpe(record: Record, dictionary: CpeDictionary) -> str | None:
    """The CPE *dictionary* suggests for *record*, or ``None``."""
    # A record read from a `.drv` or a name cannot say its language, and a name gives
    # `1.16.2-lib` as dbus's version.
    if record.origin is not Origin.VALUE or record.language:
        return None
    if record.cpe or record.possible_cpes or not record.version:
        return None
    product = bind(record.cpe_product or record.name)
    vendor = dictionary.vendors.get(product)
    if vendor is None:
        return None
    part = record.cpe_part or "a"
    return f"cpe:2.3:{part}:{vendor}:{product}:{bind(record.version)}:*:*:*:*:*:*:*"


def with_generated_cpes(listed: Inventory, dictionary: CpeDictionary) -> Inventory:
    """*listed*, each record with the CPE *dictionary* suggests for it."""

    def fill(record: Record) -> Record:
        return replace(record, generated_cpe=generated_cpe(record, dictionary))

    return replace(listed, target=fill(listed.target), components=tuple(map(fill, listed.components)))

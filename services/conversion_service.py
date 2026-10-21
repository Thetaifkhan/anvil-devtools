"""Conversion routines — JSON/XML/YAML/CSV.

XML uses lxml (required production dependency) to preserve:
  - namespace prefixes (soap: stays soap:, never rewritten to ns0:)
  - comments, CDATA, processing instructions
  - XML declaration
  - mixed-content whitespace (semantic spaces between inline elements)
  - attribute order

lxml is a HARD dependency. If unavailable the application raises ImportError
at startup rather than silently degrading to a lossy stdlib fallback.
"""
import csv
import datetime
import io
import json
import re

# lxml is REQUIRED. Do not add a try/except fallback — a silent lossy
# fallback is worse than a clear startup error.
from lxml import etree as _lxml_etree

import xml.etree.ElementTree as ET   # used only for JSON↔XML conversion helpers
from xml.dom import minidom

import yaml as pyyaml

from services.errors import ToolError

# Public flag so tests can assert lxml is always active.
_HAS_LXML = True


# ---------------------------------------------------------------------------
# Internal XML parsing helpers
# ---------------------------------------------------------------------------

def _make_lxml_parser(remove_comments=False, remove_pis=False):
    """Create a non-networked parser that does not load DTDs or resolve entities."""
    return _lxml_etree.XMLParser(
        remove_blank_text=False,   # preserve text nodes — we decide what to touch
        remove_comments=remove_comments,
        remove_pis=remove_pis,     # formatting preserves PIs; JSON conversion omits them
        strip_cdata=False,         # keep <![CDATA[...]]>
        resolve_entities=False,    # never expand external or internal entities
        no_network=True,           # do not fetch external DTDs or entities
        load_dtd=False,
        dtd_validation=False,
        attribute_defaults=False,
        huge_tree=False,           # retain libxml2's safety limits
    )


def _parse_lxml(xml_text, remove_comments=False, remove_pis=False):
    """Parse a Unicode XML string with lxml.

    Handles the encoding-declaration mismatch problem:
    The browser has already decoded the text to Unicode, so we must NOT
    re-encode with a codec that contradicts the in-document declaration.
    Strategy:
      1. Strip any encoding declaration from the prolog (we'll add it back
         after formatting if it was present).
      2. Parse using lxml's unicode_literals / fromstring path, which
         accepts a str directly without a byte-encoding step.
    """
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise ToolError("Enter some XML first.")
    if re.search(r"<!\s*DOCTYPE\b", xml_text, re.IGNORECASE):
        raise ToolError("DOCTYPE declarations and custom entities are not supported for security reasons.")
    clean_text = _strip_xml_declaration(xml_text) if _extract_xml_declaration(xml_text) else xml_text
    parser = _make_lxml_parser(remove_comments=remove_comments, remove_pis=remove_pis)
    try:
        # The input is already Unicode, and its optional declaration is stripped.
        root = _lxml_etree.fromstring(clean_text, parser)
        return root
    except _lxml_etree.XMLSyntaxError as exc:
        line = exc.lineno or 0
        col  = exc.offset or 0
        msg  = str(exc).split('\n')[0]  # first line only
        # libxml2 leaks the internal hint "use XML_PARSE_HUGE" for deep trees;
        # users don't need that wording.
        if "Excessive depth" in msg or "XML_PARSE_HUGE" in msg:
            msg = "Document exceeds the maximum supported nesting depth."
        raise ToolError(f"Invalid XML at line {line}, column {col}: {msg}")
    except (ValueError, TypeError) as exc:
        raise ToolError(f"Invalid XML: {exc}")


def _extract_xml_declaration(xml_text):
    """Return the raw <?xml ...?> declaration line if present, else None."""
    stripped = xml_text.lstrip()
    m = re.match(r'(<\?xml[^?]*\?>)', stripped, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else None


def _strip_xml_declaration(xml_text):
    """Remove the <?xml...?> prolog so lxml can parse the pure element.

    Line breaks in the removed prolog are kept as blank lines so that lxml's
    reported line numbers still match the line numbers in the user's input.
    """
    match = re.match(r'\s*<\?xml[^?]*\?>', xml_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return xml_text
    return "\n" * match.group(0).count("\n") + xml_text[match.end():]


# ---------------------------------------------------------------------------
# Mixed-content awareness
# ---------------------------------------------------------------------------

def _has_mixed_content(elem):
    """Return True if elem has child elements AND any text/tail content (even whitespace-only).

    This is the conservative definition: if an element has both child elements
    AND any text node (direct or tail), we treat it as potentially mixed-content
    and never touch its whitespace.

    Rationale: <p> <b>world</b></p> has a space-only text node before <b>.
    That space may be semantically significant (it separates inline elements).
    We cannot safely strip it without knowing the rendering context.

    Only pure structural containers — elements where elem.text is None/absent
    AND every child.tail is None/absent — are considered element-only and
    eligible for indentation reformatting.
    """
    children = list(elem)
    if not children:
        return False
    # If direct text exists at all (even whitespace-only) → mixed
    if elem.text is not None:
        return True
    # If any child has a tail at all (even whitespace-only) → mixed
    for child in children:
        if child.tail is not None:
            return True
    return False


def _is_element_only_content(elem):
    """Return True if elem contains only child elements with pure-whitespace text."""
    children = list(elem)
    if not children:
        return False
    # Direct text must be whitespace-only (or absent)
    if elem.text and elem.text.strip():
        return False
    # Every child's tail must be whitespace-only (or absent)
    for child in children:
        if child.tail and child.tail.strip():
            return False
    return True


# ---------------------------------------------------------------------------
# XML formatting / validation
# ---------------------------------------------------------------------------

def format_xml(xml_text, indent="  ", minify=False):
    """Pretty-print or minify XML with full fidelity using lxml.

    Preserves:
      - namespace prefixes exactly as written
      - comments
      - CDATA sections
      - processing instructions
      - XML declaration (with encoding normalized to UTF-8 in output)
      - mixed-content whitespace (semantic whitespace is never stripped)
      - attributes

    Mixed-content awareness:
      Elements with BOTH child elements AND significant text are treated as
      mixed-content and their whitespace is never modified.  Only element-only
      containers (structural whitespace) are reformatted.
    """
    if not xml_text or not xml_text.strip():
        raise ToolError("Enter some XML first.")

    original_decl = _extract_xml_declaration(xml_text)
    # Strip declaration before parsing so lxml doesn't try to re-encode bytes
    clean_text = _strip_xml_declaration(xml_text) if original_decl else xml_text

    root = _parse_lxml(clean_text)

    if minify:
        _lxml_minify_whitespace(root)
        body = _lxml_etree.tostring(
            root,
            encoding="unicode",
            xml_declaration=False,
            with_comments=True,
        )
        if original_decl:
            # Normalise encoding declaration to UTF-8 (output is always UTF-8)
            decl = _normalise_declaration(original_decl)
            return decl + body
        return body

    # Pretty-print: indent only element-only containers
    _lxml_pretty_indent(root, indent, level=0)
    body = _lxml_etree.tostring(
        root,
        encoding="unicode",
        xml_declaration=False,
        with_comments=True,
        pretty_print=False,   # we handle indentation ourselves
    )
    if original_decl:
        decl = _normalise_declaration(original_decl)
        return decl + "\n" + body
    return body


def _normalise_declaration(decl):
    """Rewrite encoding in declaration to UTF-8 (since output is always UTF-8 text).

    Input:  <?xml version="1.0" encoding="UTF-16"?>
    Output: <?xml version="1.0" encoding="UTF-8"?>

    If there is no encoding attribute we leave the declaration unchanged.
    """
    # Replace encoding="..." with encoding="UTF-8" case-insensitively
    normalised = re.sub(
        r'encoding\s*=\s*["\'][^"\']*["\']',
        'encoding="UTF-8"',
        decl,
        flags=re.IGNORECASE,
    )
    return normalised


def _lxml_pretty_indent(elem, indent, level, parent_is_mixed=False):
    """Recursively indent an lxml element tree.

    Rules:
      - MIXED CONTENT (text/tails with non-whitespace characters mixed with
        child elements): left completely untouched — including tails of children.
      - ELEMENT-ONLY CONTENT (structural containers with only indentation
        whitespace between child elements): whitespace is replaced with proper
        indentation.
      - LEAF elements (no children): text content untouched; tail fixed only
        when parent is element-only (not mixed).

    Idempotency guarantee:
      We detect "indent-only" whitespace (newline + spaces/tabs) and treat it
      as reformattable.  Non-indent whitespace (e.g. a single space) is treated
      as content and never touched.
    """
    children = list(elem)

    if not children:
        # Leaf: never touch text.
        # Only fix tail when our parent is element-only (not mixed).
        if not parent_is_mixed:
            if elem.tail is not None and not elem.tail.strip():
                elem.tail = "\n" + indent * level
        return

    # Determine if this is a mixed-content node.
    # Conservative rule: if the source document has ANY text or tail content
    # (even whitespace-only) alongside child elements, treat it as mixed.
    # This preserves semantically meaningful spaces like <p> <b>word</b></p>.
    # Pure structural containers (from serialisers or our own previous pretty-
    # print pass) have text/tail that is ONLY newline+indent — we detect that
    # pattern to allow idempotent re-indentation.
    def _is_indent_only(s):
        """True if s is None or contains only newline+spaces/tabs (indentation)."""
        return s is None or bool(re.match(r'^[\n\r][ \t]*$', s))

    has_mixed_text  = elem.text is not None and not _is_indent_only(elem.text)
    has_mixed_tails = any(c.tail is not None and not _is_indent_only(c.tail) for c in children)
    is_mixed = has_mixed_text or has_mixed_tails

    if is_mixed:
        # Mixed-content: recurse into children, passing that their parent is mixed
        # so they do NOT rewrite their own tails.
        for child in children:
            _lxml_pretty_indent(child, indent, level + 1, parent_is_mixed=True)
        return

    # Element-only container: replace whitespace with proper indentation.
    child_indent   = "\n" + indent * (level + 1)
    closing_indent = "\n" + indent * level

    # Rewrite whitespace-only direct text (idempotent: indent-only → rewrite)
    if elem.text is None or not elem.text.strip():
        elem.text = child_indent

    for i, child in enumerate(children):
        _lxml_pretty_indent(child, indent, level + 1, parent_is_mixed=False)
        is_last = (i == len(children) - 1)
        # Rewrite indent-only tails
        if child.tail is None or not child.tail.strip():
            child.tail = closing_indent if is_last else child_indent

    if not parent_is_mixed and (elem.tail is None or not elem.tail.strip()):
        elem.tail = "\n" + indent * level


def _lxml_minify_whitespace(elem):
    """Strip inter-element whitespace for minification.

    Mirrors the mixed-content logic in _lxml_pretty_indent.
    Significant text/tails in mixed-content elements are never removed.
    """
    children = list(elem)

    if not children:
        if elem.tail and not elem.tail.strip():
            elem.tail = None
        return

    def _is_indent_only(s):
        return s is None or bool(re.match(r'^[\n\r][ \t]*$', s))
    has_mixed_text  = elem.text is not None and not _is_indent_only(elem.text)
    has_mixed_tails = any(c.tail is not None and not _is_indent_only(c.tail) for c in children)
    is_mixed = has_mixed_text or has_mixed_tails

    if is_mixed:
        for child in children:
            _lxml_minify_whitespace(child)
        return

    # Element-only: strip all whitespace
    if elem.text and not elem.text.strip():
        elem.text = None
    for child in children:
        _lxml_minify_whitespace(child)
        if child.tail and not child.tail.strip():
            child.tail = None


def validate_xml(xml_text):
    """Validate XML. Returns {'valid', 'message', 'line', 'column'}."""
    try:
        _parse_lxml(xml_text)
        return {"valid": True, "message": "Well-formed XML."}
    except ToolError as exc:
        m = re.search(r'line (\d+), column (\d+)', exc.message)
        return {
            "valid": False,
            "message": exc.message,
            "line":   int(m.group(1)) if m else None,
            "column": int(m.group(2)) if m else None,
        }


# ---------------------------------------------------------------------------
# JSON <-> XML
# ---------------------------------------------------------------------------

def _is_valid_xml_name(name):
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(c.isalnum() or c in "-_." for c in name[1:])


def _reject_invalid_json_constants(value, path=""):
    """Walk a parsed JSON value and reject NaN/Infinity, which Python's
    json.loads accepts by default but which are not valid JSON and would
    leak into downstream tools (XML, YAML, CSV) as 'nan'/'Infinity' text."""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ToolError(
                "JSON contains NaN or Infinity literals, which are not valid JSON. "
                "Use null or a numeric value instead."
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            _reject_invalid_json_constants(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_invalid_json_constants(v, f"{path}[{i}]")
    return value


def _parse_strict_json(json_text):
    """Parse JSON, rejecting NaN/Infinity/-Infinity literals."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}.")
    return _reject_invalid_json_constants(data)


def json_to_xml(json_text, root_name="root"):
    try:
        data = _parse_strict_json(json_text)
    except ToolError:
        raise

    if not root_name or not root_name.strip():
        root_name = "root"

    if not _is_valid_xml_name(root_name):
        raise ToolError(
            f"Invalid XML root element name '{root_name}'. "
            "Names must start with a letter or underscore and contain only "
            "letters, digits, hyphens, underscores and dots."
        )

    root = ET.Element(root_name)
    _build_xml_node(root, data)
    rough = ET.tostring(root, encoding="unicode")
    try:
        pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    except Exception as exc:
        raise ToolError(f"Could not serialise to XML: {exc}")
    lines = [line for line in pretty.split("\n") if line.strip()]
    # minidom adds an XML declaration — remove it (we'll let the user control that)
    if lines and lines[0].startswith("<?xml"):
        lines = lines[1:]
    return "\n".join(lines)


def _build_xml_node(parent, value):
    if isinstance(value, dict):
        for key, val in value.items():
            safe_key = _sanitize_tag(str(key))
            child = ET.SubElement(parent, safe_key)
            _build_xml_node(child, val)
    elif isinstance(value, list):
        for item in value:
            child = ET.SubElement(parent, "item")
            _build_xml_node(child, item)
    elif value is None:
        parent.set("nil", "true")
    elif isinstance(value, bool):
        # Use JSON spelling, not Python's "True"/"False".
        parent.text = "true" if value else "false"
    else:
        parent.text = str(value)


def _sanitize_tag(name):
    cleaned = "".join(c if (c.isalnum() or c in "_-.") else "_" for c in name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def xml_to_json(xml_text):
    """Convert XML to JSON using the same hardened, non-networked parser.

    Policy for mixed content and XML features:
      - Attributes → '@attributes': {...}
      - Multiple same-name children → list
      - Text content stripped of surrounding whitespace (for simple elements)
      - Mixed content (text + child elements): '#text' key added
      - Comments and processing instructions are omitted; CDATA becomes text
    """
    if not xml_text or not xml_text.strip():
        raise ToolError("Enter some XML first.")
    clean_text = _strip_xml_declaration(xml_text) if _extract_xml_declaration(xml_text) else xml_text
    root = _parse_lxml(clean_text, remove_comments=True, remove_pis=True)
    try:
        return json.dumps(_xml_node_to_dict(root), indent=2, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ToolError(f"XML could not be represented as JSON: {exc}")


def _xml_node_to_dict(node):
    result = {}
    if node.attrib:
        result["@attributes"] = dict(node.attrib)

    children = list(node)
    if children:
        child_groups = {}
        for child in children:
            # Preserve namespace prefix exactly as written by the user
            # (matches the documented behaviour: "Namespace prefixes are
            # preserved as part of the tag name where present, but namespace
            # resolution is not performed."). Default-namespace elements
            # have no prefix and collapse to their local name.
            if child.prefix:
                tag = f"{child.prefix}:{_lxml_etree.QName(child).localname}"
            else:
                tag = _lxml_etree.QName(child).localname
            child_groups.setdefault(tag, []).append(_xml_node_to_dict(child))
        for tag, group in child_groups.items():
            result[tag] = group[0] if len(group) == 1 else group

    # Capture direct text
    text = (node.text or "").strip()
    if text:
        if result:
            result["#text"] = text
        else:
            return text

    # Capture tail text of children (mixed content). Previously only a flag
    # was set and the actual tail text was lost — now collect it so round-
    # tripping mixed content like `<p>Hello <b>world</b>!</p>` keeps the "!".
    tails = [child.tail.strip() for child in children if child.tail and child.tail.strip()]
    if tails:
        result["#tails"] = tails[0] if len(tails) == 1 else tails
        result["@mixed"] = True

    return result if result else None


# ---------------------------------------------------------------------------
# JSON <-> YAML
# ---------------------------------------------------------------------------

def json_to_yaml(json_text):
    data = _parse_strict_json(json_text)
    return pyyaml.dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _yaml_json_default(value):
    """Serialize YAML-only scalar types (dates, timestamps) as ISO 8601 strings."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} values have no JSON equivalent")


def yaml_to_json(yaml_text):
    try:
        data = pyyaml.safe_load(yaml_text)
        return json.dumps(data, indent=2, ensure_ascii=False, default=_yaml_json_default)
    except pyyaml.YAMLError as exc:
        raise ToolError(f"Invalid YAML: {exc}")
    except (TypeError, ValueError, RecursionError) as exc:
        # Covers unsupported YAML types and recursive aliases (circular structures).
        raise ToolError(f"YAML cannot be represented as JSON: {exc}")


# ---------------------------------------------------------------------------
# CSV <-> JSON
# ---------------------------------------------------------------------------

def csv_to_json(csv_text, delimiter=None):
    if delimiter is not None and (not isinstance(delimiter, str) or len(delimiter) != 1):
        raise ToolError("CSV delimiter must be exactly one character.")
    if delimiter is None:
        try:
            sample = csv_text[:2048]
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","

    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ToolError("Could not detect a header row in the CSV input.")
    column_count = len(reader.fieldnames)
    rows = []
    for row in reader:
        # DictReader files surplus values under a None key. Reject them rather
        # than emitting a JSON "null" key that hides the misaligned data.
        if None in row:
            raise ToolError(
                f"Row at line {reader.line_num} has more values than the "
                f"{column_count} header columns."
            )
        rows.append(row)
    return json.dumps(rows, indent=2, ensure_ascii=False), delimiter


def json_to_csv(json_text):
    data = _parse_strict_json(json_text)

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data or not all(isinstance(r, dict) for r in data):
        raise ToolError('JSON to CSV requires an array of objects, e.g. [{"a": 1}].')

    fieldnames = []
    for row in data:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in data:
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return buf.getvalue()


def _csv_cell(value):
    """Render a JSON value as CSV text without Python-specific spellings.

    Booleans use JSON's true/false; nested objects and arrays are kept as
    JSON text so the cell can be read back instead of a Python repr.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value

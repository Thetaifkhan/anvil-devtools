"""Single source of truth for every tool's metadata.

Every tool page, the homepage search index, the sitemap, and the nav menu
all read from this list, so adding a tool never means updating five
different places by hand.
"""

TOOLS = [
    # --- Formatter (unified workspace) ---------------------------------
    {
        "slug": "formatter",
        "name": "Formatter",
        "category": "JSON & XML",
        "short": "One workspace to beautify, minify and validate JSON, XML, SQL, YAML and Markdown.",
        "keywords": ["format", "formatter", "beautify", "pretty print", "minify", "json", "xml", "sql", "yaml", "markdown"],
        "template": "tools/formatter.html",
        "client_only": True,
    },
    # --- JSON ---------------------------------------------------------
    {
        "slug": "json-formatter",
        "name": "JSON Formatter",
        "category": "JSON & XML",
        "short": "Beautify, minify and validate JSON with configurable indentation.",
        "keywords": ["json", "beautify", "pretty print", "format", "minify"],
        "template": "tools/json_formatter.html",
        "client_only": True,
    },
    {
        "slug": "json-validator",
        "name": "JSON Validator",
        "category": "JSON & XML",
        "short": "Validate JSON syntax with precise line and column error messages.",
        "keywords": ["json", "validate", "lint", "syntax check"],
        "template": "tools/json_validator.html",
        "client_only": True,
    },
    {
        "slug": "json-tree-viewer",
        "name": "JSON Tree Viewer",
        "category": "JSON & XML",
        "short": "Explore JSON as a collapsible tree with search and JSONPath copy.",
        "keywords": ["json", "tree", "explorer", "viewer", "jsonpath"],
        "template": "tools/json_tree_viewer.html",
        "client_only": True,
    },
    {
        "slug": "json-diff",
        "name": "JSON Diff Checker",
        "category": "JSON & XML",
        "short": "Structurally compare two JSON documents and highlight changes.",
        "keywords": ["json", "diff", "compare", "changes"],
        "template": "tools/json_diff.html",
        "client_only": True,
    },
    {
        "slug": "json-to-xml",
        "name": "JSON to XML Converter",
        "category": "JSON & XML",
        "short": "Convert JSON documents into well-formed XML.",
        "keywords": ["json", "xml", "convert"],
        "template": "tools/json_to_xml.html",
        "client_only": False,
    },
    {
        "slug": "xml-to-json",
        "name": "XML to JSON Converter",
        "category": "JSON & XML",
        "short": "Convert XML documents into JSON, preserving attributes.",
        "keywords": ["xml", "json", "convert"],
        "template": "tools/xml_to_json.html",
        "client_only": False,
    },
    {
        "slug": "json-to-yaml",
        "name": "JSON to YAML Converter",
        "category": "Converters",
        "short": "Convert JSON into readable YAML.",
        "keywords": ["json", "yaml", "convert"],
        "template": "tools/json_to_yaml.html",
        "client_only": False,
    },
    {
        "slug": "yaml-to-json",
        "name": "YAML to JSON Converter",
        "category": "Converters",
        "short": "Convert YAML into JSON.",
        "keywords": ["yaml", "json", "convert"],
        "template": "tools/yaml_to_json.html",
        "client_only": False,
    },
    {
        "slug": "csv-to-json",
        "name": "CSV to JSON Converter",
        "category": "Data Tools",
        "short": "Convert CSV data into JSON with automatic delimiter detection.",
        "keywords": ["csv", "json", "convert", "spreadsheet"],
        "template": "tools/csv_to_json.html",
        "client_only": False,
    },
    {
        "slug": "json-to-csv",
        "name": "JSON to CSV Converter",
        "category": "Data Tools",
        "short": "Flatten a JSON array of objects into CSV.",
        "keywords": ["json", "csv", "convert"],
        "template": "tools/json_to_csv.html",
        "client_only": False,
    },
    # --- XML ------------------------------------------------------------
    {
        "slug": "xml-formatter",
        "name": "XML Formatter",
        "category": "JSON & XML",
        "short": "Pretty-print or minify XML with configurable indentation.",
        "keywords": ["xml", "format", "beautify", "minify"],
        "template": "tools/xml_formatter.html",
        "client_only": False,
    },
    {
        "slug": "xml-validator",
        "name": "XML Validator",
        "category": "JSON & XML",
        "short": "Check XML is well-formed with line/column error reporting.",
        "keywords": ["xml", "validate", "well-formed"],
        "template": "tools/xml_validator.html",
        "client_only": False,
    },
    # --- Encoding ---------------------------------------------------------
    {
        "slug": "base64-encode",
        "name": "Base64 Encoder",
        "category": "Encoding",
        "short": "Encode text or files to Base64, including URL-safe variants.",
        "keywords": ["base64", "encode"],
        "template": "tools/base64_encode.html",
        "client_only": True,
    },
    {
        "slug": "base64-decode",
        "name": "Base64 Decoder",
        "category": "Encoding",
        "short": "Decode Base64 back to readable text or download the file.",
        "keywords": ["base64", "decode"],
        "template": "tools/base64_decode.html",
        "client_only": True,
    },
    {
        "slug": "url-encode",
        "name": "URL Encoder / Decoder",
        "category": "Encoding",
        "short": "Percent-encode or decode URLs, components and query strings.",
        "keywords": ["url", "encode", "decode", "percent encoding", "uri"],
        "template": "tools/url_encode.html",
        "client_only": True,
    },
    {
        "slug": "html-entities",
        "name": "HTML Entity Encoder / Decoder",
        "category": "Encoding",
        "short": "Convert text to and from HTML entities.",
        "keywords": ["html", "entities", "escape", "unescape"],
        "template": "tools/html_entities.html",
        "client_only": True,
    },
    # --- Security -----------------------------------------------------
    {
        "slug": "jwt-decoder",
        "name": "JWT Decoder",
        "category": "Security",
        "short": "Decode a JWT's header, payload and claims entirely in your browser.",
        "keywords": ["jwt", "decode", "token", "claims"],
        "template": "tools/jwt_decoder.html",
        "client_only": True,
    },
    {
        "slug": "hash-generator",
        "name": "Hash Generator",
        "category": "Security",
        "short": "Generate MD5, SHA-1, SHA-256, SHA-384 and SHA-512 hashes.",
        "keywords": ["hash", "md5", "sha256", "sha1", "checksum"],
        "template": "tools/hash_generator.html",
        "client_only": True,
    },
    {
        "slug": "uuid-generator",
        "name": "UUID Generator",
        "category": "Generators",
        "short": "Generate one or many UUID v4 / v1 identifiers.",
        "keywords": ["uuid", "guid", "generator"],
        "template": "tools/uuid_generator.html",
        "client_only": True,
    },
    {
        "slug": "password-generator",
        "name": "Password Generator",
        "category": "Generators",
        "short": "Generate strong random passwords with configurable rules.",
        "keywords": ["password", "generator", "random"],
        "template": "tools/password_generator.html",
        "client_only": True,
    },
    # --- Web / API -----------------------------------------------------
    {
        "slug": "timestamp-converter",
        "name": "Unix Timestamp Converter",
        "category": "Date & Time",
        "short": "Convert between Unix timestamps and human-readable dates.",
        "keywords": ["timestamp", "unix", "epoch", "date", "convert"],
        "template": "tools/timestamp_converter.html",
        "client_only": True,
    },
    {
        "slug": "cron-parser",
        "name": "Cron Expression Parser",
        "category": "Date & Time",
        "short": "Turn a cron expression into a plain-English schedule.",
        "keywords": ["cron", "schedule", "parser"],
        "template": "tools/cron_parser.html",
        "client_only": True,
    },
    {
        "slug": "regex-tester",
        "name": "Regex Tester",
        "category": "Text Tools",
        "short": "Test regular expressions with live match and group highlighting.",
        "keywords": ["regex", "regexp", "pattern", "test"],
        "template": "tools/regex_tester.html",
        "client_only": True,
    },
    {
        "slug": "text-diff",
        "name": "Text Diff Checker",
        "category": "Text Tools",
        "short": "Compare two blocks of text line by line, side by side.",
        "keywords": ["diff", "compare", "text"],
        "template": "tools/text_diff.html",
        "client_only": True,
    },
    {
        "slug": "xml-diff",
        "name": "XML Diff Checker",
        "category": "JSON & XML",
        "short": "Compare two XML documents and highlight structural differences.",
        "keywords": ["xml", "diff", "compare", "changes"],
        "template": "tools/xml_diff.html",
        "client_only": True,
    },
    {
        "slug": "markdown-preview",
        "name": "Markdown Previewer",
        "category": "Text Tools",
        "short": "Write Markdown and preview the rendered HTML side by side.",
        "keywords": ["markdown", "preview", "html"],
        "template": "tools/markdown_preview.html",
        "client_only": True,
    },
    {
        "slug": "http-status-lookup",
        "name": "HTTP Status Code Lookup",
        "category": "API Tools",
        "short": "Look up the meaning and typical cause of any HTTP status code.",
        "keywords": ["http", "status code", "404", "500", "api"],
        "template": "tools/http_status_lookup.html",
        "client_only": True,
    },
    {
        "slug": "sql-formatter",
        "name": "SQL Formatter",
        "category": "Data Tools",
        "short": "Beautify SQL queries with consistent indentation and keyword casing.",
        "keywords": ["sql", "format", "beautify"],
        "template": "tools/sql_formatter.html",
        "client_only": False,
    },
    {
        "slug": "number-base-converter",
        "name": "Number Base Converter",
        "category": "Converters",
        "short": "Convert numbers between binary, octal, decimal and hexadecimal.",
        "keywords": ["binary", "hex", "decimal", "octal", "base converter"],
        "template": "tools/number_base_converter.html",
        "client_only": True,
    },
    {
        "slug": "color-converter",
        "name": "Color Converter",
        "category": "Web Tools",
        "short": "Convert colors between HEX, RGB and HSL with a live preview.",
        "keywords": ["color", "hex", "rgb", "hsl", "css"],
        "template": "tools/color_converter.html",
        "client_only": True,
    },
]

CATEGORIES = [
    "JSON & XML",
    "Encoding",
    "Security",
    "API Tools",
    "Text Tools",
    "Generators",
    "Converters",
    "Web Tools",
    "Date & Time",
    "Data Tools",
]

POPULAR_SLUGS = [
    "formatter",
    "json-formatter",
    "jwt-decoder",
    "base64-encode",
    "uuid-generator",
    "regex-tester",
    "timestamp-converter",
    "json-diff",
    "hash-generator",
]


CAPABILITY_MAP = {
    # slug: (operation, [formats], [actions])
    "formatter":              ("formatter",  ["json", "xml", "yaml", "sql", "markdown"], ["format", "minify", "validate"]),
    "json-formatter":         ("formatter",  ["json"], ["format", "minify"]),
    "json-validator":         ("validator",  ["json"], ["validate"]),
    "json-tree-viewer":       ("viewer",     ["json"], ["view"]),
    "json-diff":              ("diff",       ["json"], ["compare"]),
    "json-to-xml":            ("converter",  ["json", "xml"], ["convert"]),
    "xml-to-json":            ("converter",  ["xml", "json"], ["convert"]),
    "json-to-yaml":           ("converter",  ["json", "yaml"], ["convert"]),
    "yaml-to-json":           ("converter",  ["yaml", "json"], ["convert"]),
    "csv-to-json":            ("converter",  ["csv", "json"], ["convert"]),
    "json-to-csv":            ("converter",  ["json", "csv"], ["convert"]),
    "xml-formatter":          ("formatter",  ["xml"], ["format", "minify"]),
    "xml-validator":          ("validator",  ["xml"], ["validate"]),
    "base64-encode":          ("encoder",    ["base64"], ["encode"]),
    "base64-decode":          ("decoder",    ["base64"], ["decode"]),
    "url-encode":             ("encoder",    ["url"], ["encode", "decode"]),
    "html-entities":          ("encoder",    ["html-entity"], ["encode", "decode"]),
    "jwt-decoder":            ("decoder",    ["jwt"], ["decode"]),
    "hash-generator":         ("generator",  ["hash"], ["generate"]),
    "uuid-generator":         ("generator",  ["uuid"], ["generate"]),
    "password-generator":     ("generator",  ["password"], ["generate"]),
    "timestamp-converter":    ("converter",  ["timestamp"], ["convert"]),
    "cron-parser":            ("tester",     ["cron"], ["parse"]),
    "regex-tester":           ("tester",     ["regex"], ["test"]),
    "text-diff":              ("diff",       ["text"], ["compare"]),
    "xml-diff":               ("diff",       ["xml"], ["compare"]),
    "markdown-preview":       ("formatter",  ["markdown"], ["preview"]),
    "http-status-lookup":     ("tester",     ["http-status"], ["lookup"]),
    "sql-formatter":          ("formatter",  ["sql"], ["format", "minify"]),
    "number-base-converter":  ("converter",  ["number-base"], ["convert"]),
    "color-converter":        ("converter",  ["color"], ["convert"]),
}

for _tool in TOOLS:
    _op, _formats, _actions = CAPABILITY_MAP.get(_tool["slug"], ("tool", [], []))
    _tool["operation"] = _op
    _tool["formats"] = _formats
    _tool["actions"] = _actions

OPERATIONS = [
    "formatter", "validator", "converter", "encoder", "decoder",
    "diff", "generator", "viewer", "parser", "tester",
]


def capability_index():
    """Compact tool list for the Workbench operation/format selectors and
    the command palette — everything they need, nothing they don't."""
    return [
        {
            "id": t["slug"],
            "slug": t["slug"],
            "name": t["name"],
            "operation": t["operation"],
            "formats": t["formats"],
            "actions": t["actions"],
            "category": t["category"],
            "short": t["short"],
        }
        for t in TOOLS
    ]


def tools_for_operation(operation):
    return [t for t in TOOLS if t["operation"] == operation]


def get_tool(slug):
    for tool in TOOLS:
        if tool["slug"] == slug:
            return tool
    return None


def tools_by_category():
    grouped = {c: [] for c in CATEGORIES}
    for tool in TOOLS:
        grouped.setdefault(tool["category"], []).append(tool)
    return grouped


def popular_tools():
    lookup = {t["slug"]: t for t in TOOLS}
    return [lookup[s] for s in POPULAR_SLUGS if s in lookup]


# (operation, format) pairs that the Workbench page actually supports.
# Mirrors the OPS map in static/js/workbench.js — keep in sync.
# Tools whose (operation, format) pair isn't in this set should NOT show
# the "Open in Workbench →" link, because the workbench would silently
# fall back to a different tool and confuse the user.
WORKBENCH_SUPPORTED = {
    ("formatter", "json"), ("formatter", "xml"), ("formatter", "yaml"),
    ("formatter", "sql"), ("formatter", "markdown"),
    ("validator", "json"), ("validator", "xml"), ("validator", "yaml"),
    ("converter", "json-xml"), ("converter", "json-yaml"), ("converter", "csv-json"),
    ("encoder", "base64"), ("encoder", "url"), ("encoder", "html-entity"),
    ("decoder", "base64"), ("decoder", "jwt"), ("decoder", "url"), ("decoder", "html-entity"),
    ("diff", "json"), ("diff", "text"),
    ("generator", "uuid"), ("generator", "password"), ("generator", "hash"),
    ("viewer", "json"),
    ("tester", "regex"), ("tester", "cron"), ("tester", "http-status"),
}


def workbench_supports(tool):
    """True if the Workbench page has a panel for this tool's
    (operation, first-format) pair. Drives whether the per-tool page
    shows the "Open in Workbench →" link."""
    if not tool:
        return False
    op = tool.get("operation")
    formats = tool.get("formats") or []
    if not op or not formats:
        return False
    # The Workbench uses composite format keys like "json-xml" for converters,
    # but CAPABILITY_MAP stores them as ["json", "xml"]. Build the composite
    # key for converters so the lookup matches the OPS map in workbench.js.
    for fmt in formats:
        if (op, fmt) in WORKBENCH_SUPPORTED:
            return True
    # Special-case: converters use a composite "src-dst" key derived from
    # the (operation, formats[0], formats[1]) tuple.
    if op == "converter" and len(formats) >= 2:
        composite = f"{formats[0]}-{formats[1]}"
        if (op, composite) in WORKBENCH_SUPPORTED:
            return True
    return False

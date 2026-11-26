from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from analytics import limiter_identity
from services import conversion_service as conv
from services import sql_service
from services.errors import ToolError
from services.rate_limit import CONVERT_LIMITER

convert_bp = Blueprint("convert_tools", __name__, url_prefix="/api")


def _check_rate_limit():
    """Reject a caller that exceeds the per-IP conversion limit.

    Returns a 429 response (with the standard Retry-After header) or
    None when the caller is allowed through.
    """
    key = limiter_identity(
        request.headers.get("X-Forwarded-For", ""),
        request.remote_addr or "",
    )
    allowed, retry_after = CONVERT_LIMITER.check(key)
    if allowed:
        return None
    resp = jsonify({
        "ok": False,
        "error": "Too many conversion requests from your address. "
                 "Please wait a moment and try again.",
    })
    resp.status_code = 429
    resp.headers["Retry-After"] = str(max(1, int(retry_after)))
    return resp


def _text_from_request():
    """Extract and validate the required text field from a JSON object."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ToolError("Request body must be a JSON object with a 'text' field.")

    if "text" not in payload:
        raise ToolError("Missing required field: 'text'.")

    text = payload["text"]
    if not isinstance(text, str):
        raise ToolError(f"Field 'text' must be a string, not {type(text).__name__}.")

    # Use the configured limit consistently so deployments can tune memory and
    # request-processing costs without changing application code.
    max_chars = current_app.config.get("MAX_TEXT_CHARS", 2_000_000)
    if len(text) > max_chars:
        raise ToolError(f"Input is too large to process (limit: {max_chars:,} characters).")

    return text, payload


@convert_bp.post("/json-to-xml")
def api_json_to_xml():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, payload = _text_from_request()
        root_name = payload.get("root", "root")
        if not isinstance(root_name, str):
            raise ToolError(f"Field 'root' must be a string, not {type(root_name).__name__}.")
        result = conv.json_to_xml(text, root_name)
        return jsonify({"ok": True, "result": result})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        # Flask errors (413 body too large, 404, ...) keep their status.
        raise
    except Exception:
        # Unexpected server error
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500


@convert_bp.post("/xml-to-json")
def api_xml_to_json():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, _ = _text_from_request()
        result = conv.xml_to_json(text)
        return jsonify({"ok": True, "result": result})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        raise
    except Exception:
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500


@convert_bp.post("/xml-format")
def api_xml_format():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, payload = _text_from_request()
        minify = payload.get("minify", False)
        if not isinstance(minify, bool):
            raise ToolError(f"Field 'minify' must be a boolean, not {type(minify).__name__}.")
        indent = payload.get("indent", "  ")
        if not isinstance(indent, str):
            raise ToolError(f"Field 'indent' must be a string, not {type(indent).__name__}.")
        if indent != "\t" and (len(indent) > 16 or any(char != " " for char in indent)):
            raise ToolError("Field 'indent' must be 0–16 spaces or one tab.")
        result = conv.format_xml(text, indent=indent, minify=minify)
        return jsonify({"ok": True, "result": result})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        raise
    except Exception:
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500


@convert_bp.post("/xml-validate")
def api_xml_validate():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, _ = _text_from_request()
        result = conv.validate_xml(text)
        return jsonify({"ok": True, **result})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        raise
    except Exception:
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500


@convert_bp.post("/json-to-yaml")
def api_json_to_yaml():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, _ = _text_from_request()
        result = conv.json_to_yaml(text)
        return jsonify({"ok": True, "result": result})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        raise
    except Exception:
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500


@convert_bp.post("/yaml-to-json")
def api_yaml_to_json():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, _ = _text_from_request()
        result = conv.yaml_to_json(text)
        return jsonify({"ok": True, "result": result})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        raise
    except Exception:
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500


@convert_bp.post("/csv-to-json")
def api_csv_to_json():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, payload = _text_from_request()
        delimiter = payload.get("delimiter") or None
        if delimiter is not None and not isinstance(delimiter, str):
            raise ToolError(f"Field 'delimiter' must be a string, not {type(delimiter).__name__}.")
        result, used_delim = conv.csv_to_json(text, delimiter)
        return jsonify({"ok": True, "result": result, "delimiter": used_delim})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        raise
    except Exception:
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500


@convert_bp.post("/json-to-csv")
def api_json_to_csv():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, _ = _text_from_request()
        result = conv.json_to_csv(text)
        return jsonify({"ok": True, "result": result})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        raise
    except Exception:
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500


@convert_bp.post("/sql-format")
def api_sql_format():
    limited = _check_rate_limit()
    if limited is not None:
        return limited
    try:
        text, payload = _text_from_request()
        action = payload.get("action", "format")

        # Validate action
        if action not in ("format", "minify"):
            raise ToolError(f"Invalid action '{action}'. Must be 'format' or 'minify'.")

        if action == "minify":
            strip_comments = payload.get("strip_comments", False)
            if not isinstance(strip_comments, bool):
                raise ToolError("Field 'strip_comments' must be a boolean.")
            result = sql_service.minify_sql(text, strip_comments=strip_comments)
        else:
            keyword_case = payload.get("keyword_case", "upper")
            if keyword_case not in ("upper", "lower", "capitalize"):
                raise ToolError(f"Invalid keyword_case '{keyword_case}'. Must be 'upper', 'lower' or 'capitalize'.")

            indent_width_raw = payload.get("indent_width", 2)
            if not isinstance(indent_width_raw, int) or isinstance(indent_width_raw, bool):
                raise ToolError(f"Field 'indent_width' must be an integer, not {type(indent_width_raw).__name__}.")
            indent_width = indent_width_raw

            if indent_width < 0 or indent_width > 16:
                raise ToolError("Field 'indent_width' must be between 0 and 16.")

            result = sql_service.format_sql(text, indent_width=indent_width, keyword_case=keyword_case)
        return jsonify({"ok": True, "result": result})
    except ToolError as e:
        return jsonify({"ok": False, "error": e.message}), 400
    except HTTPException:
        raise
    except Exception:
        return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500

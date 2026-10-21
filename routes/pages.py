from flask import Blueprint, abort, render_template, current_app

from tool_registry import TOOLS, CATEGORIES, tools_by_category, popular_tools, get_tool, capability_index, OPERATIONS

pages_bp = Blueprint("pages", __name__)


@pages_bp.get("/")
def home():
    return render_template(
        "index.html",
        categories=CATEGORIES,
        grouped=tools_by_category(),
        popular=popular_tools(),
        all_tools=TOOLS,
        tool_count=len(TOOLS),
    )


@pages_bp.get("/workbench")
def workbench():
    import json as _json
    return render_template(
        "pages/workbench.html",
        operations=OPERATIONS,
        capability_json=(
            _json.dumps(capability_index(), ensure_ascii=False)
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        ),
    )


@pages_bp.get("/<slug>")
def tool_page(slug):
    tool = get_tool(slug)
    if tool is None:
        abort(404)
    related = [t for t in TOOLS if t["category"] == tool["category"] and t["slug"] != slug][:5]
    return render_template(
        tool["template"],
        tool=tool,
        related=related,
        site_url=current_app.config["SITE_URL"],
    )


@pages_bp.get("/privacy-policy")
def privacy_policy():
    return render_template("pages/privacy.html")


@pages_bp.get("/terms-of-service")
def terms():
    return render_template("pages/terms.html")


@pages_bp.get("/disclaimer")
def disclaimer():
    return render_template("pages/disclaimer.html")


@pages_bp.get("/contact")
def contact():
    return render_template("pages/contact.html")


@pages_bp.get("/tools")
def tool_directory():
    return render_template(
        "pages/directory.html",
        categories=CATEGORIES,
        grouped=tools_by_category(),
    )

"""Doc pointers must resolve.

The CLI, runtime and MCP surfaces print things like "see docs/HOSTING.md". In a checkout those paths
resolve; from `pip install survey-agent` there is no checkout, so the pointer has to degrade to a URL
rather than a dead end. And a doc rename must not silently turn every printed pointer into a 404 —
that's what the drift guards below are for."""
import os
import re

from survey_agent import docs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src", "survey_agent")


def _shipped_py_files():
    for base, _dirs, files in os.walk(SRC):
        if "__pycache__" in base:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def test_every_doc_named_in_shipped_code_exists():
    """A renamed or deleted doc makes every runtime message that names it a dead link."""
    missing = {}
    for path in _shipped_py_files():
        with open(path, encoding="utf-8") as fh:
            for ref in set(re.findall(r"docs/[A-Za-z0-9_\-]+\.md", fh.read())):
                if not os.path.isfile(os.path.join(REPO_ROOT, ref)):
                    missing.setdefault(ref, []).append(os.path.relpath(path, REPO_ROOT))
    assert not missing, f"shipped code points at docs that don't exist: {missing}"


def test_every_entry_in_the_doc_map_exists():
    missing = [(name, path) for name, (path, _) in docs.DOCS.items()
               if not os.path.isfile(os.path.join(REPO_ROOT, path))]
    assert not missing, f"docs.DOCS points at missing files: {missing}"


def test_doc_map_covers_every_doc_named_in_shipped_code():
    """If the code tells a user to read a doc, `survey-agent docs` must be able to hand it to them."""
    mapped = {path for path, _ in docs.DOCS.values()}
    named = set()
    for path in _shipped_py_files():
        with open(path, encoding="utf-8") as fh:
            named |= set(re.findall(r"docs/[A-Za-z0-9_\-]+\.md", fh.read()))
    assert not (named - mapped), f"named in code but not in the doc map: {sorted(named - mapped)}"


def test_resolve_prefers_a_local_file_in_a_checkout():
    kind, loc = docs.resolve("hosting")
    assert kind == "file" and loc.endswith("docs/HOSTING.md") and os.path.isfile(loc)


def test_url_is_always_absolute_and_covers_unmapped_paths():
    assert docs.url("hosting").startswith("https://")
    assert docs.url("docs/HOSTING.md") == docs.url("hosting")
    assert docs.url("docs/NOT_A_DOC.md").startswith("https://")


def test_read_returns_text_for_a_local_doc():
    assert "survey" in (docs.read("logic") or "").lower()


def test_index_lines_render_one_entry_per_doc():
    lines = docs.index_lines()
    assert len(lines) == len(docs.DOCS)
    for name in docs.DOCS:
        assert any(name in ln for ln in lines), name


def test_help_output_has_no_bare_doc_paths():
    """`survey-agent help` used to end by naming docs/*.md files that a pip install doesn't have."""
    import io
    from contextlib import redirect_stdout
    from survey_agent import cli
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main(["help"])
    out = buf.getvalue()
    assert "survey-agent docs" in out
    bare = [m for m in re.findall(r"docs/[A-Za-z0-9_\-]+\.md", out)]
    assert not bare, f"help still names doc paths that don't exist in a pip install: {bare}"


# --------------------------------------------------------------------------- the docs themselves

DOC_ROOT_FILES = ("README.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md",
                  "CODE_OF_CONDUCT.md", "CHANGELOG.md", "demo-survey-agent.md", "llms.txt",
                  os.path.join("templates", "README.md"))
REPO_URL_BLOB = "https://github.com/paulneto/survey-agent/blob/main/"
REPO_URL_TREE = "https://github.com/paulneto/survey-agent/tree/main/"
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE = re.compile(r"^(```|~~~)")


def _markdown_docs():
    out = [os.path.join(REPO_ROOT, f) for f in DOC_ROOT_FILES]
    for sub in ("docs", "skills"):
        for base, _dirs, files in os.walk(os.path.join(REPO_ROOT, sub)):
            out += [os.path.join(base, f) for f in files if f.endswith(".md")]
    return [p for p in out if os.path.isfile(p)]


def _slug(heading):
    """GitHub's heading anchor: lowercase, drop punctuation (keep word chars, '-' and spaces),
    spaces → '-'."""
    h = re.sub(r"`|\*\*|\*|_(?=\W)|\[([^\]]*)\]\([^)]*\)", r"\1", heading.strip().lower())
    h = re.sub(r"[^\w\- ]", "", h, flags=re.UNICODE)
    return h.replace(" ", "-")


def _anchors(path):
    anchors, seen, fenced = set(), {}, False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if _FENCE.match(line.strip()):
                fenced = not fenced
                continue
            m = None if fenced else re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
            if m:
                s = _slug(m.group(1))
                n = seen.get(s, 0)
                anchors.add(s if n == 0 else f"{s}-{n}")
                seen[s] = n + 1
    return anchors


def _links(path):
    fenced = False
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if _FENCE.match(line.strip()):
                fenced = not fenced
                continue
            if fenced:
                continue
            for target in _LINK.findall(re.sub(r"`[^`]*`", "", line)):
                yield lineno, target


def test_every_relative_link_and_anchor_in_the_docs_resolves():
    """A doc link that 404s on GitHub (or an anchor that no longer exists) is a broken promise to the
    reader. Absolute links into this repo are checked against the checkout too."""
    broken = []
    for doc in _markdown_docs():
        here = os.path.dirname(doc)
        for lineno, target in _links(doc):
            if target.startswith((REPO_URL_BLOB, REPO_URL_TREE)):
                rel = target.split("/main/", 1)[1]
                path_part, _, anchor = rel.partition("#")
                dest = os.path.join(REPO_ROOT, path_part)
            elif re.match(r"^[a-z]+:", target):
                continue                                   # other external links aren't checked here
            else:
                path_part, _, anchor = target.partition("#")
                dest = os.path.normpath(os.path.join(here, path_part)) if path_part else doc
            where = f"{os.path.relpath(doc, REPO_ROOT)}:{lineno} → {target}"
            if not os.path.exists(dest):
                broken.append(where + " (missing file)")
            elif anchor and dest.endswith(".md") and anchor not in _anchors(dest):
                broken.append(where + " (missing anchor)")
    assert not broken, "broken doc links:\n  " + "\n  ".join(broken)


def test_docs_index_links_every_mapped_doc():
    """`survey-agent docs` and docs/README.md are two views of one map; keep them in step."""
    with open(os.path.join(REPO_ROOT, "docs", "README.md"), encoding="utf-8") as fh:
        index = fh.read()
    unlinked = []
    for name, (path, _) in docs.DOCS.items():
        if path == "docs/README.md":
            continue
        rel = os.path.relpath(os.path.join(REPO_ROOT, path), os.path.join(REPO_ROOT, "docs"))
        if f"]({rel}" not in index:
            unlinked.append(path)
    assert not unlinked, f"docs/README.md doesn't link these mapped docs: {unlinked}"

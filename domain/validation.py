import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from domain.errors import HarnessError


def validate_css(source: str):
    # Block escaped CSS too: otherwise url()/@import can be obfuscated.
    if re.search(r"url\s*\(|@import|expression\s*\(|javascript:|\\", source, re.I):
        raise HarnessError("UNSAFE_CONTENT", "External resources and executable CSS are not allowed")


class StaticHTML(HTMLParser):
    tags = set(
        "html head body title meta style div span p b strong em i u s h1 h2 h3 h4 h5 h6 ul ol li table thead tbody tr th td caption details summary label input br hr pre code blockquote section article figure figcaption main header footer button sub sup".split()
    )
    attrs = set(
        "class id style title role aria-label aria-expanded colspan rowspan type name checked open for lang dir charset content http-equiv".split()
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            raise HarnessError("UNSAFE_CONTENT", f"HTML tag {tag} is not allowed")
        for key, value in attrs:
            if key not in self.attrs or key.startswith("on"):
                raise HarnessError("UNSAFE_CONTENT", "HTML attribute is not allowed")
            if key == "style":
                validate_css(value or "")
            if key == "http-equiv" or (
                tag == "input" and key == "type" and value not in ("radio", "checkbox")
            ):
                raise HarnessError("UNSAFE_CONTENT", "Only static card interactions are allowed")
        if tag not in ("br", "hr", "meta", "input"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise HarnessError("INVALID_HTML", "Unbalanced HTML tags")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "hr", "meta", "input"):
            self.stack.pop()


def validate_html(source: str):
    validate_css(source)
    parser = StaticHTML(convert_charrefs=True)
    parser.feed(source)
    parser.close()
    if parser.stack:
        raise HarnessError("INVALID_HTML", "Unclosed HTML tags")


def validate_svg(source: str):
    if re.search(r"<!DOCTYPE|<!ENTITY", source, re.I):
        raise HarnessError("UNSAFE_CONTENT")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise HarnessError("INVALID_SVG") from exc
    allowed = set(
        "svg g defs path rect circle ellipse line polyline polygon text tspan title desc style clipPath mask linearGradient radialGradient stop pattern symbol use marker".split()
    )
    if root.tag.split("}")[-1] != "svg":
        raise HarnessError("INVALID_SVG")
    for node in root.iter():
        if node.tag.split("}")[-1] not in allowed:
            raise HarnessError("UNSAFE_CONTENT")
        for key, value in node.attrib.items():
            key = key.split("}")[-1]
            if key.lower().startswith("on") or (key == "href" and not value.startswith("#")):
                raise HarnessError("UNSAFE_CONTENT")
            # Allow internal paint references, never external resources.
            cleaned = re.sub(r"url\(#[a-zA-Z0-9_-]+\)", "", value)
            validate_css(cleaned)
        if node.tag.split("}")[-1] == "style":
            validate_css(node.text or "")


def validate_latex(source: str):
    if re.search(
        r"\\(?:href|url|html\w*|require|newcommand|def|include|input|write|catcode)\b", source, re.I
    ):
        raise HarnessError("UNSAFE_CONTENT", "External or executable TeX extensions are not allowed")
    depth = 0
    for match in re.finditer(r"(?<!\\)[{}]", source):
        depth += 1 if match[0] == "{" else -1
        if depth < 0:
            raise HarnessError("INVALID_LATEX", "Unbalanced TeX braces")
    if depth:
        raise HarnessError("INVALID_LATEX", "Unbalanced TeX braces")
    environments = []
    allowed = {
        "aligned",
        "alignedat",
        "gathered",
        "cases",
        "array",
        "matrix",
        "pmatrix",
        "bmatrix",
        "Bmatrix",
        "vmatrix",
        "Vmatrix",
        "smallmatrix",
        "align",
        "align*",
        "gather",
        "gather*",
        "equation",
        "equation*",
    }
    for action, name in re.findall(r"\\(begin|end)\{([^}]+)\}", source):
        if name not in allowed:
            raise HarnessError("INVALID_LATEX", "Unsupported TeX environment")
        if action == "begin":
            environments.append(name)
        elif not environments or environments.pop() != name:
            raise HarnessError("INVALID_LATEX", "Unbalanced TeX environment")
    if environments:
        raise HarnessError("INVALID_LATEX", "Unclosed TeX environment")
    commands = set(
        "alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega frac dfrac tfrac binom dbinom tbinom sqrt text textrm textbf textit mathrm mathbf mathit mathsf mathtt mathcal mathbb mathscr operatorname overline underline hat widehat bar vec dot ddot tilde widetilde acute grave breve check overset underset overbrace underbrace boxed begin end left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr sum prod coprod int iint iiint oint oiint oiiint lim limsup liminf sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh log ln lg exp max min sup inf det gcd Pr mod bmod pmod in notin ni subset supset subseteq supseteq subsetneq supsetneq cup cap setminus emptyset varnothing forall exists nexists neg land lor lnot implies iff to mapsto gets rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow longrightarrow longleftarrow Longrightarrow Longleftarrow Longleftrightarrow uparrow downarrow le leq ge geq ne neq approx sim simeq cong equiv propto parallel perp mid nmid pm mp times div cdot cdots ldots vdots ddots dots infty partial nabla ell hbar Re Im degree angle triangle prime circ bullet star ast backslash vert Vert lvert rvert lVert rVert langle rangle lbrace rbrace lfloor rfloor lceil rceil quad qquad displaystyle textstyle scriptstyle scriptscriptstyle limits nolimits substack phantom vphantom hphantom color small tiny large Large footnotesize rm bf it sf tt cal".split()
    )
    # Base + AMS commands implemented by the client's bundled MathJax v4.
    commands.update(
        "checkmark blacksquare square xrightarrow xleftarrow nRightarrow nLeftarrow nLeftrightarrow tag notag".split()
    )
    for match in re.finditer(r"\\([A-Za-z]+)", source):
        if match[1] not in commands:
            raise HarnessError("INVALID_LATEX", f"Unsupported MathJax command: \\{match[1]}")
    delimiter_depth = 0
    for action in re.findall(r"\\(left|right)\b", source):
        delimiter_depth += 1 if action == "left" else -1
        if delimiter_depth < 0:
            raise HarnessError("INVALID_LATEX", "Unbalanced TeX delimiters")
    if delimiter_depth:
        raise HarnessError("INVALID_LATEX", "Unbalanced TeX delimiters")
    # Check common argument-taking commands; balanced braces alone would let
    # "\frac" through even though MathJax cannot typeset it.
    arities = {
        "frac": 2,
        "dfrac": 2,
        "tfrac": 2,
        "binom": 2,
        "overset": 2,
        "underset": 2,
        "sqrt": 1,
        "text": 1,
        "mathrm": 1,
        "mathbf": 1,
        "mathbb": 1,
        "overline": 1,
        "underline": 1,
        "vec": 1,
        "hat": 1,
        "boxed": 1,
        "xrightarrow": 1,
        "xleftarrow": 1,
        "tag": 1,
    }
    for match in re.finditer(r"\\([A-Za-z]+)", source):
        remaining = source[match.end() :].lstrip()
        if match[1] in ("sqrt", "xrightarrow", "xleftarrow") and remaining.startswith("["):
            index_end = remaining.find("]")
            if index_end == -1:
                raise HarnessError("INVALID_LATEX", "Unclosed root index")
            remaining = remaining[index_end + 1 :].lstrip()
        for _ in range(arities.get(match[1], 0)):
            if not remaining or remaining[0] in "}":
                raise HarnessError("INVALID_LATEX", "Missing TeX argument")
            if remaining[0] == "{":
                level, end = 0, None
                for token in re.finditer(r"(?<!\\)[{}]", remaining):
                    level += 1 if token[0] == "{" else -1
                    if level == 0:
                        end = token.end()
                        break
                if end is None:
                    raise HarnessError("INVALID_LATEX", "Invalid TeX argument")
                remaining = remaining[end:].lstrip()
            elif remaining[0] == "\\":
                command = re.match(r"\\(?:[A-Za-z]+|.)", remaining)
                if not command:
                    raise HarnessError("INVALID_LATEX", "Invalid TeX argument")
                remaining = remaining[command.end() :].lstrip()
            else:
                remaining = remaining[1:].lstrip()

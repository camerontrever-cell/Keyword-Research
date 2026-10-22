"""
Minimal, dependency-free XLSX writer (stdlib only).

Why: the sandbox does not persist pip installs between sessions, so the whole
app is built on the Python standard library. This writer produces real
Office Open XML workbooks with styled headers, frozen panes, autofilters,
column widths, number formats and per-cell fills -- enough for a
production-looking SEO deliverable.

Usage
-----
    from xlsx_writer import Workbook
    wb = Workbook()
    s = wb.add_sheet("Master", freeze="A2", autofilter=True,
                     widths={"A": 42, "B": 12})
    s.header(["Keyword", "Volume"])
    s.row(["dental clinic jodhpur", 1900], styles=["text", "int"])
    s.row(["clear aligners cost", 320], styles=["text", "int", ])
    wb.save("out.xlsx")

Cell values may also be dicts: {"v": 12.5, "s": "money"}.
"""

from __future__ import annotations

import datetime as _dt
import re
import zipfile
from xml.sax.saxutils import escape

# --------------------------------------------------------------------------
# Style registry
# --------------------------------------------------------------------------

# (font_id, fill_id, border_id, numFmt_id, align_id)
_STYLE_DEFS = {
    # name          font fill border numfmt align
    "default":      (0, 0, 0, 0, 0),
    "header":       (1, 2, 1, 0, 1),
    "text":         (0, 0, 0, 0, 0),
    "bold":         (2, 0, 0, 0, 0),
    "int":          (0, 0, 0, 3, 2),
    "num":          (0, 0, 0, 4, 2),
    "pct":          (0, 0, 0, 5, 2),
    "money":        (0, 0, 0, 6, 2),
    "score":        (2, 0, 0, 3, 2),
    "wrap":         (0, 0, 0, 0, 3),
    "date":         (0, 0, 0, 1, 2),
    # tinted fills for prioritisation
    "green":        (0, 3, 0, 0, 0),
    "green_int":    (0, 3, 0, 3, 2),
    "green_score":  (2, 3, 0, 3, 2),
    "amber":        (0, 4, 0, 0, 0),
    "amber_int":    (0, 4, 0, 3, 2),
    "amber_score":  (2, 4, 0, 3, 2),
    "red":          (0, 5, 0, 0, 0),
    "red_int":      (0, 5, 0, 3, 2),
    "red_score":    (2, 5, 0, 3, 2),
    "muted":        (3, 0, 0, 0, 0),
    "muted_wrap":   (3, 0, 0, 0, 3),
    "title":        (4, 0, 0, 0, 0),
    "section":      (2, 6, 0, 0, 0),
}

_FONTS = [
    '<font><sz val="10"/><name val="Calibri"/><color rgb="FF1F2430"/></font>',                     # 0 body
    '<font><b/><sz val="10"/><name val="Calibri"/><color rgb="FFFFFFFF"/></font>',                  # 1 header
    '<font><b/><sz val="10"/><name val="Calibri"/><color rgb="FF1F2430"/></font>',                  # 2 bold
    '<font><sz val="10"/><name val="Calibri"/><color rgb="FF7A8194"/></font>',                      # 3 muted
    '<font><b/><sz val="15"/><name val="Calibri"/><color rgb="FF123A6B"/></font>',                  # 4 title
]

_FILLS = [
    '<fill><patternFill patternType="none"/></fill>',
    '<fill><patternFill patternType="gray125"/></fill>',
    '<fill><patternFill patternType="solid"><fgColor rgb="FF1F3864"/><bgColor indexed="64"/></patternFill></fill>',
    '<fill><patternFill patternType="solid"><fgColor rgb="FFD8EFDF"/><bgColor indexed="64"/></patternFill></fill>',
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFDF0CE"/><bgColor indexed="64"/></patternFill></fill>',
    '<fill><patternFill patternType="solid"><fgColor rgb="FFF9D7D5"/><bgColor indexed="64"/></patternFill></fill>',
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE8EDF6"/><bgColor indexed="64"/></patternFill></fill>',
]

_BORDERS = [
    "<border/>",
    '<border><left style="thin"><color rgb="FFB4BBCB"/></left>'
    '<right style="thin"><color rgb="FFB4BBCB"/></right>'
    '<top style="thin"><color rgb="FFB4BBCB"/></top>'
    '<bottom style="thin"><color rgb="FFB4BBCB"/></bottom></border>',
]

_NUMFMTS = [
    "General",
    "yyyy-mm-dd",
    "#,##0",
    "#,##0",          # 3 int
    "#,##0.00",       # 4 num
    "0.0%",           # 5 pct
    '"$"#,##0.00',    # 6 money
]

_ALIGNS = [
    "",
    '<alignment horizontal="center" vertical="center" wrapText="1"/>',
    '<alignment horizontal="right"/>',
    '<alignment vertical="top" wrapText="1"/>',
]

_STYLE_IDS = {name: i for i, name in enumerate(_STYLE_DEFS)}

_ILLEGAL = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def _clean(s: str) -> str:
    s = _ILLEGAL.sub("", str(s))
    if len(s) > 32000:
        s = s[:32000]
    return escape(s)


def col_letter(idx: int) -> str:
    """1-based column index -> Excel letters."""
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


# --------------------------------------------------------------------------
# Sheet
# --------------------------------------------------------------------------
class Sheet:
    def __init__(self, name, freeze=None, autofilter=False, widths=None,
                 row_height=None, merge_title=None):
        self.name = name
        self.freeze = freeze
        self.autofilter = autofilter
        self.widths = widths or {}
        self.row_height = row_height
        self.rows: list[list[dict]] = []

    # -- writing helpers ---------------------------------------------------
    def _cell(self, value, style):
        if isinstance(value, dict):
            return value
        return {"v": value, "s": style}

    def header(self, values, style="header"):
        self.rows.append([self._cell(v, style) for v in values])
        return self

    def row(self, values, styles=None):
        styles = styles or []
        out = []
        for i, v in enumerate(values):
            out.append(self._cell(v, styles[i] if i < len(styles) else "text"))
        self.rows.append(out)
        return self

    def blank(self):
        self.rows.append([])
        return self

    def note(self, text, style="muted_wrap", span=8):
        cells = [{"v": text, "s": style}]
        cells += [{"v": None, "s": "default"} for _ in range(span - 1)]
        self.rows.append(cells)
        return self

    # -- rendering ---------------------------------------------------------
    def _render(self):
        freeze = ""
        if self.freeze:
            col, row = self.freeze[0], int(self.freeze[1:])
            pane = (f'<pane xSplit="{ord(col.upper()) - 64}" ySplit="{row}" '
                    f'topLeftCell="{self.freeze}" activePane="bottomRight" state="frozen"/>')
            freeze = pane

        cols_xml = ""
        if self.widths:
            parts = []
            for letters, width in sorted(self.widths.items(),
                                         key=lambda kv: len(kv[0]) * 100 + ord(kv[0][-1])):
                parts.append(f'<col min="{_col_idx(letters)}" max="{_col_idx(letters)}" '
                             f'width="{width}" customWidth="1"/>')
            cols_xml = "<cols>" + "".join(parts) + "</cols>"

        sheet_data = []
        for r_i, row in enumerate(self.rows, start=1):
            if not row:
                sheet_data.append(f'<row r="{r_i}"/>')
                continue
            cells = []
            for c_i, cell in enumerate(row, start=1):
                v = cell.get("v")
                s = _STYLE_IDS.get(cell.get("s", "text"), 0)
                ref = f"{col_letter(c_i)}{r_i}"
                if v is None or v == "":
                    if s:
                        cells.append(f'<c r="{ref}" s="{s}"/>')
                    continue
                if isinstance(v, bool):
                    cells.append(f'<c r="{ref}" s="{s}" t="b"><v>{int(v)}</v></c>')
                elif isinstance(v, (_dt.date, _dt.datetime)):
                    serial = (v - _dt.date(1899, 12, 30)).days
                    cells.append(f'<c r="{ref}" s="{s}"><v>{serial}</v></c>')
                elif isinstance(v, (int, float)):
                    cells.append(f'<c r="{ref}" s="{s}"><v>{v}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" s="{s}" t="inlineStr">'
                                 f'<is><t xml:space="preserve">{_clean(v)}</t></is></c>')
            sheet_data.append(f'<row r="{r_i}">' + "".join(cells) + "</row>")

        af = ""
        if self.autofilter and self.rows:
            last = f"{col_letter(max(len(r) for r in self.rows))}{len(self.rows)}"
            af = f'<autoFilter ref="A1:{last}"/>'

        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheetViews><sheetView workbookViewId="0">' + freeze +
            '</sheetView></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/>'
            + cols_xml +
            "<sheetData>" + "".join(sheet_data) + "</sheetData>"
            + af +
            '<pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
            "</worksheet>"
        )


def _col_idx(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


# --------------------------------------------------------------------------
# Workbook
# --------------------------------------------------------------------------
class Workbook:
    def __init__(self):
        self.sheets: list[Sheet] = []
        self.props = {"title": "Keyword Research", "creator": "Keyword Studio"}

    def add_sheet(self, name, **kw) -> Sheet:
        s = Sheet(_safe_sheet_name(name), **kw)
        self.sheets.append(s)
        return s

    def save(self, path):
        n = len(self.sheets)
        ct = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, n + 1))
            + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            "</Types>"
        )
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            "</Relationships>"
        )
        workbook = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            + "".join(
                f'<sheet name="{_clean(s.name)}" sheetId="{i}" r:id="rId{i}"/>'
                for i, s in enumerate(self.sheets, start=1))
            + "</sheets></workbook>"
        )
        wb_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, n + 1))
            + f'<Relationship Id="rId{n + 1}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                'Target="styles.xml"/>'
            "</Relationships>"
        )

        styles = self._styles_xml()
        core = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{_clean(self.props["title"])}</dc:title>'
            f'<dc:creator>{_clean(self.props["creator"])}</dc:creator>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{_dt.datetime.now().isoformat(timespec="seconds")}</dcterms:created>'
            "</cp:coreProperties>"
        )
        app = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>Keyword Studio</Application></Properties>"
        )

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", ct)
            z.writestr("_rels/.rels", rels)
            z.writestr("docProps/core.xml", core)
            z.writestr("docProps/app.xml", app)
            z.writestr("xl/workbook.xml", workbook)
            z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
            z.writestr("xl/styles.xml", styles)
            for i, s in enumerate(self.sheets, start=1):
                z.writestr(f"xl/worksheets/sheet{i}.xml", s._render())
        return path

    def _styles_xml(self):
        xfs = []
        for name, (f, fill, b, nf, al) in _STYLE_DEFS.items():
            xfs.append(
                f'<xf numFmtId="{_NUMFMT_IDS[nf]}" fontId="{f}" fillId="{fill}" borderId="{b}" '
                f'xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyNumberFormat="1"'
                + (' applyAlignment="1" >' + _ALIGNS[al] + "</xf>" if al else "/>")
            )
        # duplicate numFmt ids beyond builtin 0..6 handled via custom block
        # only custom numFmt ids (>=164) need declaring; 0/3/4/14 are built in
        custom = [(164, "0.0%"), (165, '"$"#,##0.00')]
        custom = [(i, escape(f, {'"': "&quot;"})) for i, f in custom]
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + (f'<numFmts count="{len(custom)}">'
               + "".join(f'<numFmt numFmtId="{i}" formatCode="{f}"/>' for i, f in custom)
               + "</numFmts>" if custom else "")
            + f'<fonts count="{len(_FONTS)}">' + "".join(_FONTS) + "</fonts>"
            + f'<fills count="{len(_FILLS)}">' + "".join(_FILLS) + "</fills>"
            + f'<borders count="{len(_BORDERS)}">' + "".join(_BORDERS) + "</borders>"
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(xfs)}">' + "".join(xfs) + "</cellXfs>"
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"
        )


# builtin numFmt ids: 0 General, 1 "0", 2 "0.00", 3 "#,##0", 4 "#,##0.00"
# custom ids start at 164
_NUMFMT_IDS = {
    0: 0,      # General
    1: 14,     # date
    2: 3,      # "#,##0"
    3: 3,      # int (builtin #,##0)
    4: 4,      # num (builtin #,##0.00)
    5: 164,    # pct custom "0.0%"
    6: 165,    # money custom
}


def _safe_sheet_name(name: str) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "-", str(name)).strip()
    return name[:31] or "Sheet"


if __name__ == "__main__":
    wb = Workbook()
    s = wb.add_sheet("Demo", freeze="A2", autofilter=True,
                     widths={"A": 40, "B": 12, "C": 10})
    s.header(["Keyword", "Volume", "KD"])
    s.row(["dental implant jodhpur", 1300, 24], styles=["text", "int", "green_int"])
    s.row(["clear aligners cost india", 880, 41], styles=["text", "int", "amber_int"])
    s.note("Smoke test sheet")
    print(wb.save("/tmp/_t.xlsx"))

import re
from bs4 import BeautifulSoup

_DECIMAL_RE = re.compile(r'(?<=\d),(?=\d)')
def fix_french_decimals(text):
    return _DECIMAL_RE.sub('.', text)

def old_serialize(table_html):
    soup = BeautifulSoup(table_html, "html.parser")
    return soup.get_text(separator=" ", strip=True)

def html_table_to_grid(table_html):
    soup = BeautifulSoup(f"<table>{table_html}</table>", "html.parser")
    rows = soup.find_all("tr")
    grid, is_header, occupied = [], [], {}

    for r, tr in enumerate(rows):
        while len(grid) <= r:
            grid.append([]); is_header.append([])
        c = 0
        for cell in tr.find_all(["td", "th"]):
            while occupied.get((r, c)):
                c += 1
            text = fix_french_decimals(cell.get_text(strip=True))
            rowspan = int(cell.get("rowspan", 1) or 1)
            colspan = int(cell.get("colspan", 1) or 1)
            header = cell.name == "th"
            for dr in range(rowspan):
                rr = r + dr
                while len(grid) <= rr:
                    grid.append([]); is_header.append([])
                for dc in range(colspan):
                    cc = c + dc
                    while len(grid[rr]) <= cc:
                        grid[rr].append(""); is_header[rr].append(False)
                    grid[rr][cc] = text
                    is_header[rr][cc] = header
                    occupied[(rr, cc)] = True
            c += colspan

    width = max(len(row) for row in grid) if grid else 0
    for row, hdr in zip(grid, is_header):
        while len(row) < width:
            row.append(""); hdr.append(False)
    return grid, is_header

def build_column_headers(grid, is_header):
    # Only trust genuine <th> rows as headers. No guessing based on row 0 content.
    header_rows = [i for i, hdr_row in enumerate(is_header)
                   if any(hdr_row) and all(h or c == "" for h, c in zip(hdr_row, grid[i]))]
    if not header_rows:
        return [], 0
    n_cols = len(grid[0])
    col_headers = []
    for c in range(n_cols):
        parts = []
        for r in header_rows:
            val = grid[r][c]
            if val and (not parts or parts[-1] != val):
                parts.append(val)
        col_headers.append(" > ".join(parts))
    return col_headers, max(header_rows) + 1

def serialize_table_html_v3(table_html):
    grid, is_header = html_table_to_grid(table_html)
    if not grid:
        return ""
    col_headers, data_start = build_column_headers(grid, is_header)

    # Safety net: if header-detection consumed every row (data_start reaches the
    # end of the grid), the source HTML mistagged a data row as a header somewhere.
    # Fall back to no-header mode over the FULL grid rather than returning nothing.
    if data_start >= len(grid):
        col_headers, data_start = [], 0

    sentences = []
    for row in grid[data_start:]:
        if not any(cell.strip() for cell in row):
            continue
        if col_headers:
            pairs = [f"{h}: {v.strip()}" if h else v.strip()
                     for h, v in zip(col_headers, row) if v.strip()]
        else:
            # No trusted <th> header exists (or the safety net triggered):
            # emit plain comma-joined cell values, no fabricated labels
            pairs = [v.strip() for v in row if v.strip()]
        if pairs:
            sentences.append(", ".join(pairs) + ".")
    return " ".join(sentences)

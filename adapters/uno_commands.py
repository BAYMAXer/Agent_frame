"""LibreOffice UNO 脚本：在 VM 里直写单元格 / 程序化保存文档。

移植自 Agent-S3 的 grounding.py（SET_CELL_VALUES_CMD / SAVE_DOC_CMD / LIST_OPEN_DOCS_CMD）。
这些不是 pyautogui 动作，而是通过 controller 在目标机里用 `python -c` 执行的 UNO 脚本：
连 LibreOffice 的 2002 端口，直写单元格并 store() 落盘。

注意：SET_CELL_VALUES_CMD / SAVE_DOC_CMD 会被 .format(cell_values/app_name/sheet_name) 填充，
所以脚本里所有字面量花括号都要写成 {{ / }}，只有 {cell_values}/{app_name}/{sheet_name} 保留单花括号。
"""
from __future__ import annotations

SET_CELL_VALUES_CMD = """import uno
import subprocess
import unicodedata, json

def identify_document_type(component):
    if component.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        return "Calc"

    if component.supportsService("com.sun.star.text.TextDocument"):
        return "Writer"

    if component.supportsService("com.sun.star.sheet.PresentationDocument"):
        return "Impress"

    return None

def _norm_name(s):
    if s is None:
        return None
    if "\\\\u" in s or "\\\\U" in s or "\\\\x" in s:
        try:
            s = json.loads(f"{{s}}")
        except Exception:
            try:
                s = s.encode("utf-8").decode("unicode_escape")
            except Exception:
                pass
    return unicodedata.normalize("NFC", s)

def cell_ref_to_indices(cell_ref):
    column_letters = ''.join(filter(str.isalpha, cell_ref))
    row_number = ''.join(filter(str.isdigit, cell_ref))

    col = sum((ord(char.upper()) - ord('A') + 1) * (26**idx) for idx, char in enumerate(reversed(column_letters))) - 1
    row = int(row_number) - 1
    return col, row

def set_cell_values(new_cell_values, app_name="Untitled 1", sheet_name="Sheet1"):
    app_name = _norm_name(app_name)
    sheet_name = _norm_name(sheet_name)

    new_cell_values_idx = {{}}
    for k, v in new_cell_values.items():
        try:
            col, row = cell_ref_to_indices(k)
        except Exception:
            col = row = None

        if col is not None and row is not None:
            new_cell_values_idx[(col, row)] = v

    try:
        subprocess.run(
            'echo "password" | sudo -S ss --kill --tcp state TIME-WAIT sport = :2002',
            shell=True, check=True, text=True, capture_output=True, timeout=15
        )
    except Exception:
        pass

    subprocess.run(
        ["soffice", "--accept=socket,host=localhost,port=2002;urp;StarOffice.Service"]
    )

    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_context
    )
    context = resolver.resolve(
        "uno:socket,host=localhost,port=2002;urp;StarOffice.ComponentContext"
    )
    desktop = context.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", context
    )

    documents = []
    for i, component in enumerate(desktop.Components):
        title = component.Title
        doc_type = identify_document_type(component)
        documents.append((i, component, title, doc_type))

    spreadsheet = [doc for doc in documents if doc[3] == "Calc"]
    selected_spreadsheet = [doc for doc in spreadsheet if doc[2] == app_name]
    if spreadsheet:
        try:
            if selected_spreadsheet:
                spreadsheet = selected_spreadsheet[0][1]
            else:
                spreadsheet = spreadsheet[0][1]

            sheet = spreadsheet.Sheets.getByName(sheet_name)
        except Exception:
            raise ValueError(f"Could not find sheet {{sheet_name}} in {{app_name}}.")

        for (col, row), value in new_cell_values_idx.items():
            cell = sheet.getCellByPosition(col, row)

            if isinstance(value, (int, float)):
                cell.Value = value
            elif isinstance(value, str):
                if value.startswith("="):
                    cell.Formula = value
                else:
                    cell.String = value
            elif isinstance(value, bool):
                cell.Value = 1 if value else 0
            elif value is None:
                cell.clearContents(0)
            else:
                raise ValueError(f"Unsupported cell value type: {{type(value)}}")

        spreadsheet.store()

    else:
        raise ValueError(f"Could not find LibreOffice Calc app corresponding to {{app_name}}.")

set_cell_values(new_cell_values={cell_values}, app_name="{app_name}", sheet_name="{sheet_name}")
"""


SAVE_DOC_CMD = """import uno
import subprocess


def save_current_document(app_name=None):
    try:
        subprocess.run(
            ["soffice", "--accept=socket,host=localhost,port=2002;urp;StarOffice.Service"],
            capture_output=True, timeout=10
        )
    except Exception:
        pass

    ctx = uno.getComponentContext()
    resolver = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", ctx
    )
    context = resolver.resolve(
        "uno:socket,host=localhost,port=2002;urp;StarOffice.ComponentContext"
    )
    desktop = context.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", context
    )

    docs = []
    for comp in desktop.Components:
        title = ""
        try:
            title = comp.Title
        except Exception:
            pass
        docs.append((title, comp))

    doc = None
    if app_name:
        for title, comp in docs:
            if title == app_name:
                doc = comp
                break
    if doc is None:
        cur = desktop.CurrentComponent
        if cur is not None:
            doc = cur
    if doc is None and docs:
        doc = docs[0][1]

    if doc is None:
        raise RuntimeError("No open document to save")

    doc.store()
    try:
        print(f"STORED: {{doc.Title}}")
    except Exception:
        print("STORED")


save_current_document(app_name={app_name})
"""


LIST_OPEN_DOCS_CMD = """import uno
import subprocess


def list_open_docs():
    try:
        subprocess.run(
            ["soffice", "--accept=socket,host=localhost,port=2002;urp;StarOffice.Service"],
            capture_output=True, timeout=10
        )
    except Exception:
        pass

    ctx = uno.getComponentContext()
    resolver = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", ctx
    )
    context = resolver.resolve(
        "uno:socket,host=localhost,port=2002;urp;StarOffice.ComponentContext"
    )
    desktop = context.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", context
    )
    found = False
    for i, comp in enumerate(desktop.Components):
        title = ""
        url = ""
        try:
            title = comp.Title
        except Exception:
            pass
        try:
            url = comp.URL
        except Exception:
            pass
        print(f"{i}: title={title!r} url={url!r}")
        found = True
    if not found:
        print("NO_OPEN_DOCS")


list_open_docs()
"""


# 校验：读指定单元格的值，确认 set_cell_values 是否真的写进去了
VERIFY_CELL_CMD = """import uno
import subprocess

def verify_cell(cell_ref, app_name="Untitled 1", sheet_name="Sheet1"):
    try:
        subprocess.run(
            ["soffice", "--accept=socket,host=localhost,port=2002;urp;StarOffice.Service"],
            capture_output=True, timeout=10
        )
    except Exception:
        pass

    ctx = uno.getComponentContext()
    resolver = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", ctx
    )
    context = resolver.resolve(
        "uno:socket,host=localhost,port=2002;urp;StarOffice.ComponentContext"
    )
    desktop = context.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", context
    )

    docs = [c for c in desktop.Components if c.supportsService("com.sun.star.sheet.SpreadsheetDocument")]
    if not docs:
        print("NO_SPREADSHEET")
        return
    spreadsheet = docs[0]
    for c in docs:
        if getattr(c, "Title", "") == app_name:
            spreadsheet = c
            break

    sheet = spreadsheet.Sheets.getByName(sheet_name)
    col_letters = ''.join(filter(str.isalpha, cell_ref))
    row_number = ''.join(filter(str.isdigit, cell_ref))
    col = sum((ord(ch.upper()) - ord('A') + 1) * (26**i) for i, ch in enumerate(reversed(col_letters))) - 1
    row = int(row_number) - 1
    cell = sheet.getCellByPosition(col, row)
    print("CELL_VALUE=" + repr(cell.Value) + " STRING=" + repr(cell.String) + " FORMULA=" + repr(cell.Formula))


verify_cell(cell_ref="{cell_ref}", app_name="{app_name}", sheet_name="{sheet_name}")
"""


# 新建 sheet（LibreOffice Calc），确定性操作，不靠 GUI 点 + 号
CREATE_SHEET_CMD = """import uno
import subprocess

def create_sheet(sheet_name="Sheet2", app_name="Untitled 1"):
    try:
        subprocess.run(
            ["soffice", "--accept=socket,host=localhost,port=2002;urp;StarOffice.Service"],
            capture_output=True, timeout=10
        )
    except Exception:
        pass

    ctx = uno.getComponentContext()
    resolver = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", ctx
    )
    context = resolver.resolve(
        "uno:socket,host=localhost,port=2002;urp;StarOffice.ComponentContext"
    )
    desktop = context.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", context
    )

    docs = [c for c in desktop.Components if c.supportsService("com.sun.star.sheet.SpreadsheetDocument")]
    if not docs:
        print("NO_SPREADSHEET")
        return
    spreadsheet = docs[0]
    for c in docs:
        if getattr(c, "Title", "") == app_name:
            spreadsheet = c
            break

    if spreadsheet.Sheets.hasByName(sheet_name):
        print(f"SHEET_EXISTS: {sheet_name}")
    else:
        spreadsheet.Sheets.insertNewByName(sheet_name, 0)
        print(f"SHEET_CREATED: {sheet_name}")
    spreadsheet.store()


create_sheet(sheet_name="{sheet_name}", app_name="{app_name}")
"""

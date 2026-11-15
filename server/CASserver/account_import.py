"""Read bounded CSV and Excel tables containing CAS credentials."""

import csv
import posixpath
import re
from contextlib import closing
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
from zlib import error as ZipDecodeError

from .store import canonical_campus_number, validate_password


MAX_UPLOAD_BYTES = 256 * 1024
MAX_ACCOUNTS = 100
MAX_EXPANDED_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 128
HEADERS = ({"学工号", "学号", "工号", "campus_number", "账号", "username"}, {"密码", "password"})
CELL_REF = re.compile(r"([A-Z]{1,3})[1-9][0-9]{0,6}\Z")
EXCEL_ESCAPE = re.compile(r"_x([a-fA-F0-9]{4})_")


class AccountTableError(ValueError):
    pass


@dataclass(frozen=True)
class AccountRow:
    number: int
    username: str
    password: str = field(repr=False)


class _NoDTD(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise AccountTableError("Excel 表格含有不支持的 XML 文档类型")


def _csv_rows(raw):
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError:
        raise AccountTableError("请将 CSV 另存为 UTF-8 格式") from None
    reader = csv.reader(StringIO(content, newline=""), strict=True)
    while True:
        number = reader.line_num + 1
        try:
            cells = next(reader)
        except StopIteration:
            return
        except csv.Error:
            raise AccountTableError(f"第 {number} 行 CSV 格式有误") from None
        yield number, dict(enumerate(cells))


def _xml(archive, name):
    return ET.fromstring(archive.read(name), parser=ET.XMLParser(target=_NoDTD()))


def _part(relation):
    target = relation.get("Target", "")
    name = posixpath.normpath(target[1:] if target.startswith("/") else "xl/" + target)
    if relation.get("TargetMode") == "External" or not name.startswith("xl/"):
        raise AccountTableError("Excel 表格引用无效")
    return name


def _text(element):
    if element is None:
        return ""
    parts = element.findall("./{*}t") + element.findall("./{*}r/{*}t")
    value = EXCEL_ESCAPE.sub(lambda match: chr(int(match[1], 16)), "".join(part.text or "" for part in parts))
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def _cell_value(cell, strings):
    if cell.find("./{*}f") is not None:
        return None
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return _text(cell.find("./{*}is"))
    value = cell.findtext("./{*}v", "")
    if kind == "s":
        if not re.fullmatch(r"[0-9]{1,8}", value) or int(value) >= len(strings):
            return None
        return strings[int(value)]
    return value if kind in {"str", "n"} else None


def _sheet_rows(sheet, strings):
    number = 0
    for row in sheet.findall("./{*}sheetData/{*}row"):
        next_number = int(row.get("r", number + 1))
        if not number < next_number <= 1_048_576:
            raise AccountTableError("Excel 工作表的行号无效")
        number, column, values = next_number, -1, {}
        for cell in row.findall("./{*}c"):
            address = cell.get("r")
            if address:
                match = CELL_REF.fullmatch(address)
                if match is None:
                    raise AccountTableError(f"第 {number} 行 Excel 单元格位置无效")
                column = 0
                for letter in match[1]:
                    column = column * 26 + ord(letter) - ord("A") + 1
                column -= 1
            else:
                column += 1
            if column in values:
                raise AccountTableError(f"第 {number} 行有重复单元格")
            values[column] = _cell_value(cell, strings)
        yield number, values


def _xlsx_rows(raw):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries) > MAX_PACKAGE_ENTRIES or len({entry.filename for entry in entries}) != len(entries)
                or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES):
                raise AccountTableError("Excel 表格内容过大或格式有误")
            book = _xml(archive, "xl/workbook.xml")
            relations = _xml(archive, "xl/_rels/workbook.xml.rels")
            first = book.find("./{*}sheets/{*}sheet")
            if first is None:
                raise AccountTableError("Excel 文件没有工作表")
            relation_id = next((value for key, value in first.attrib.items() if key.endswith("}id")), "")
            worksheet = next((item for item in relations
                              if item.get("Id") == relation_id and item.get("Type", "").endswith("/worksheet")), None)
            if worksheet is None:
                raise AccountTableError("请将账号表放在 Excel 的第一张工作表")
            shared = next((item for item in relations if item.get("Type", "").endswith("/sharedStrings")), None)
            strings = [] if shared is None else [_text(item) for item in _xml(archive, _part(shared)).findall("./{*}si")]
            yield from _sheet_rows(_xml(archive, _part(worksheet)), strings)
    except AccountTableError:
        raise
    except (BadZipFile, ET.ParseError, KeyError, ValueError, OSError, RuntimeError, ZipDecodeError):
        raise AccountTableError("Excel 表格无法读取，请保存为有效的 .xlsx 文件") from None


def read_account_table(raw, file_format):
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise AccountTableError("请选择不超过 256 KiB 的非空表格")
    parsers = {"csv": _csv_rows, "xlsx": _xlsx_rows}
    if file_format not in parsers:
        raise AccountTableError("请选择 .xlsx 或 UTF-8 .csv 表格")
    rows = parsers[file_format](raw)
    with closing(rows):
        header_row, header = next(rows, (0, {}))
        columns = [[column for column, text in header.items()
                    if isinstance(text, str) and text.strip().lower() in names] for names in HEADERS]
        if header_row != 1 or any(len(matches) != 1 for matches in columns):
            raise AccountTableError("第 1 行须各有一个「学工号」和「密码」列")
        user_column, password_column = (matches[0] for matches in columns)

        accounts, seen = [], set()
        for number, cells in rows:
            username, password = cells.get(user_column, ""), cells.get(password_column, "")
            if username == password == "":
                continue
            if not isinstance(username, str) or not isinstance(password, str):
                raise AccountTableError(f"第 {number} 行请填写学工号和密码的原始值")
            try:
                name = canonical_campus_number(username)
                validate_password(password)
            except ValueError as exc:
                raise AccountTableError(f"第 {number} 行：{exc}") from None
            if name in seen:
                raise AccountTableError(f"第 {number} 行的学工号在表格中重复")
            if len(accounts) >= MAX_ACCOUNTS:
                raise AccountTableError(f"第 {number} 行超过单次导入 {MAX_ACCOUNTS} 个账号的上限")
            accounts.append(AccountRow(number, name, password))
            seen.add(name)
        if not accounts:
            raise AccountTableError("表格没有可导入的账号")
        return accounts

import unittest
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from server.CASserver.account_import import AccountTableError, read_account_table


SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def workbook(rows, *, prefix=""):
    raw = BytesIO()
    with ZipFile(raw, "w", ZIP_DEFLATED) as file:
        file.writestr("[Content_Types].xml", """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
            <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
            <Default Extension="xml" ContentType="application/xml"/>
            <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
            <Override PartName="/xl/worksheets/accounts.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
            <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
            </Types>""")
        file.writestr("_rels/.rels", f"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>""")
        file.writestr("xl/workbook.xml", f"""<workbook xmlns="{SHEET_NS}" xmlns:r="{REL_NS}">
            <sheets><sheet name="CAS" sheetId="1" r:id="rId1"/></sheets></workbook>""")
        file.writestr("xl/_rels/workbook.xml.rels", f"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1" Type="{REL_NS}/worksheet" Target="/xl/worksheets/accounts.xml"/>
            <Relationship Id="rId2" Type="{REL_NS}/sharedStrings" Target="sharedStrings.xml"/>
            </Relationships>""")
        file.writestr("xl/sharedStrings.xml", f"""<sst xmlns="{SHEET_NS}">
            <si><t>密码</t></si><si><t>学工号</t></si>
            <si><r><t>001</t></r><r><t>23</t></r></si>
            </sst>""")
        file.writestr("xl/worksheets/accounts.xml", f"""{prefix}<worksheet xmlns="{SHEET_NS}"><sheetData>
            <row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>
            {rows}</sheetData></worksheet>""")
    return raw.getvalue()


class AccountImportTests(unittest.TestCase):
    def test_csv_accepts_excel_utf8_export_and_preserves_password_text(self):
        data = '\ufeff学工号,密码\r\nCAMPUS-01," pass,word9 "\r\n\r\n'.encode()
        row, = read_account_table(data, "csv")
        self.assertEqual((row.number, row.username, row.password), (2, "campus-01", " pass,word9 "))

    def test_xlsx_handles_shared_and_inline_strings_and_sparse_columns(self):
        data = workbook("""<row r="3"><c r="A3" t="inlineStr"><is><t>pass_x005F_x0041_9</t></is></c>
            <c r="C3" t="s"><v>2</v></c></row>""")
        row, = read_account_table(data, "xlsx")
        self.assertEqual((row.number, row.username, row.password), (3, "00123", "pass_x0041_9"))

    def test_reports_the_row_of_duplicate_or_unusable_credentials(self):
        with self.assertRaisesRegex(AccountTableError, "第 3 行.*重复"):
            read_account_table(b"username,password\nAccount01,example11\naccount01,example22\n", "csv")
        with self.assertRaisesRegex(AccountTableError, "第 2 行.*密码"):
            read_account_table(b'username,password\nAccount01,"example11\nsecond line"\n', "csv")
        with self.assertRaisesRegex(AccountTableError, "第 2 行.*学工号"):
            read_account_table(b"campus_number,password\nlegacy-alias,example11\n", "csv")
        with self.assertRaisesRegex(AccountTableError, "第 3 行.*原始值"):
            read_account_table(workbook("""<row r="3"><c r="A3" t="str"><f>1+1</f><v>example11</v></c>
                <c r="C3" t="s"><v>2</v></c></row>"""), "xlsx")

    def test_rejects_dtd_in_excel_xml(self):
        with self.assertRaisesRegex(AccountTableError, "XML"):
            read_account_table(workbook("", prefix="<!DOCTYPE worksheet>"), "xlsx")

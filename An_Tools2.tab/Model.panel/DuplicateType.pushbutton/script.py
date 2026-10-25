# -*- coding: utf-8 -*-
__title__ = "Duplicate\nTypes"
__doc__ = ("Duplicate type của 1 family theo danh sách tên, phân tách bằng ';'.\n"
           "- Chọn sẵn 1 element/type trước khi chạy để dùng type đó làm nguồn,\n"
           "  hoặc chọn type nguồn từ danh sách.\n"
           "- Hoạt động cả trong Project và Family Editor.")

from pyrevit import revit, DB, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# ---------------- Helpers ----------------
def get_type_name(t):
    p = t.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
    if p and p.AsString():
        return p.AsString()
    try:
        return DB.Element.Name.GetValue(t)
    except Exception:
        return t.Name


def get_family_name(t):
    try:
        return t.FamilyName or ""
    except Exception:
        return ""


def parse_names(raw):
    """Tách chuỗi theo ';', bỏ khoảng trắng, bỏ rỗng, bỏ trùng (giữ thứ tự)."""
    result, seen = [], set()
    for n in raw.replace("\r", "").replace("\n", ";").split(";"):
        n = n.strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            result.append(n)
    return result


INVALID_CHARS = set('\\:{}[]|;<>?`~')


def is_valid_name(n):
    return not any(c in INVALID_CHARS for c in n)


def get_sibling_type_names(src_type):
    """Tên các type cùng family (hoặc cùng system family) với type nguồn."""
    names = set()
    if isinstance(src_type, DB.FamilySymbol):
        for sid in src_type.Family.GetFamilySymbolIds():
            names.add(get_type_name(doc.GetElement(sid)).lower())
    else:
        fam = get_family_name(src_type)
        for t in DB.FilteredElementCollector(doc).OfClass(src_type.GetType()):
            if get_family_name(t) == fam:
                names.add(get_type_name(t).lower())
    return names


def get_source_type_from_selection():
    ids = list(uidoc.Selection.GetElementIds())
    if len(ids) != 1:
        return None
    el = doc.GetElement(ids[0])
    if isinstance(el, DB.ElementType):
        return el
    tid = el.GetTypeId()
    if tid and tid != DB.ElementId.InvalidElementId:
        return doc.GetElement(tid)
    return None


class TypeItem(forms.TemplateListItem):
    @property
    def name(self):
        t = self.item
        cat = t.Category.Name if t.Category else "-"
        return "{} | {} : {}".format(cat, get_family_name(t), get_type_name(t))


def pick_source_type():
    types = []
    for t in DB.FilteredElementCollector(doc).WhereElementIsElementType():
        if t.Category is None or not get_family_name(t):
            continue
        if isinstance(t, (DB.FamilySymbol, DB.HostObjAttributes,
                          DB.MEPCurveType)) or t.CanBeCopied:
            types.append(t)
    types.sort(key=lambda x: (x.Category.Name, get_family_name(x), get_type_name(x)))
    if not types:
        forms.alert("Không tìm thấy type nào trong model.", exitscript=True)
    return forms.SelectFromList.show(
        [TypeItem(t) for t in types],
        title="Chọn TYPE nguồn để duplicate",
        multiselect=False, button_name="Chọn")


def ask_names(prompt_title):
    raw = forms.ask_for_string(
        prompt="Nhập tên type mới, cách nhau bằng dấu ';'\n"
               "VD: DN50;DN65;DN80",
        title=prompt_title)
    if not raw:
        script.exit()
    names = parse_names(raw)
    if not names:
        forms.alert("Danh sách tên trống.", exitscript=True)
    return names


# ---------------- Family Editor ----------------
def run_in_family_doc():
    fm = doc.FamilyManager
    cur = fm.CurrentType
    if cur is None:
        forms.alert("Family chưa có type nào. Tạo 1 type trước.", exitscript=True)
    names = ask_names("Duplicate từ type: {}".format(cur.Name))
    existing = set(ft.Name.lower() for ft in fm.Types)

    created, skipped = [], []
    with revit.Transaction("Duplicate Family Types"):
        for n in names:
            if not is_valid_name(n):
                skipped.append((n, "Tên chứa ký tự không hợp lệ"))
                continue
            if n.lower() in existing:
                skipped.append((n, "Đã tồn tại"))
                continue
            try:
                fm.CurrentType = cur          # luôn duplicate từ type gốc
                fm.NewType(n)                 # NewType copy giá trị của CurrentType
                existing.add(n.lower())
                created.append(n)
            except Exception as ex:
                skipped.append((n, str(ex)))
        fm.CurrentType = cur
    report(cur.Name, created, skipped)


# ---------------- Project ----------------
def run_in_project():
    src = get_source_type_from_selection() or pick_source_type()
    if not src:
        script.exit()
    src_name = get_type_name(src)
    names = ask_names("Duplicate từ: {} : {}".format(get_family_name(src), src_name))
    existing = get_sibling_type_names(src)

    created, skipped = [], []
    with revit.Transaction("Duplicate Types"):
        for n in names:
            if not is_valid_name(n):
                skipped.append((n, "Tên chứa ký tự không hợp lệ"))
                continue
            if n.lower() in existing:
                skipped.append((n, "Đã tồn tại"))
                continue
            try:
                new_t = src.Duplicate(n)
                existing.add(n.lower())
                created.append((n, new_t.Id))
            except Exception as ex:
                skipped.append((n, str(ex)))
    report("{} : {}".format(get_family_name(src), src_name), created, skipped)


# ---------------- Report ----------------
def report(src_label, created, skipped):
    output.print_md("## Duplicate Types")
    output.print_md("**Type nguồn:** {}".format(src_label))
    output.print_md("**Đã tạo:** {} | **Bỏ qua:** {}".format(len(created), len(skipped)))
    if created:
        output.print_md("### Đã tạo")
        for c in created:
            if isinstance(c, tuple):
                print("- {}  (ID: {})".format(c[0], output.linkify(c[1])))
            else:
                print("- {}".format(c))
    if skipped:
        output.print_md("### Bỏ qua")
        for n, why in skipped:
            print("- {}  →  {}".format(n, why))


if doc.IsFamilyDocument:
    run_in_family_doc()
else:
    run_in_project()

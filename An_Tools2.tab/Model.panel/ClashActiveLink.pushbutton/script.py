# -*- coding: utf-8 -*-
__title__ = "Clash\nActive vs Link"
__doc__ = ("Clash check: active model (Pipes horizontal / vertical, Pipe Fittings, "
           "Pipe Accessories, Plumbing Fixtures + any added category) vs linked "
           "Revit models. Results (clickable IDs) are printed to the pyRevit output "
           "window. Settings of the last run are remembered.")

import clr
import os
import json
import math
import System

clr.AddReference('PresentationCore')
clr.AddReference('PresentationFramework')
clr.AddReference('WindowsBase')

from System.Collections.Generic import List
from System.Windows import Thickness, TextWrapping, VerticalAlignment
from System.Windows.Controls import CheckBox, TextBlock, Button, DockPanel, Dock
from System.Windows.Input import Cursors
from System.Windows.Media import Brushes
from System.Windows.Threading import Dispatcher, DispatcherPriority

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    Options, ViewDetailLevel, Solid, GeometryInstance, SolidUtils, XYZ, Outline,
    BoundingBoxIntersectsFilter, ElementIntersectsSolidFilter,
    ElementMulticategoryFilter, BooleanOperationsUtils, BooleanOperationsType,
    Element, RevitLinkInstance, CategoryType)

from pyrevit import forms, script

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

XAML_FILE = os.path.join(os.path.dirname(__file__), "ui.xaml")

FT3_PER_CM3 = 1.0 / 28316.846592

# Pipe is VERTICAL when its angle to the horizontal plane >= this value.
# Everything below (flat + sloped, incl. 1:40, 1:60, 1:100 ...) = HORIZONTAL.
VERTICAL_MIN_ANGLE_DEG = 45.0
_VERT_SIN = math.sin(math.radians(VERTICAL_MIN_ANGLE_DEG))

CONFIG_FILE = os.path.join(
    System.Environment.GetFolderPath(System.Environment.SpecialFolder.ApplicationData),
    "pyRevit", "clash_active_link_config.json")


# ----------------------------------------------------------------------------
# Categories
# ----------------------------------------------------------------------------
def _bic_int(name):
    b = getattr(BuiltInCategory, name, None)
    return int(b) if b is not None else None


def _bic_ints(names):
    return set(v for v in (_bic_int(n) for n in names) if v is not None)


PIPE_CID = _bic_int("OST_PipeCurves")

# key, label, category id, pipe direction ("H" / "V" / None)
DEFAULT_ITEMS = [
    ("PIPE_H", u"Pipes - Horizontal (sloped + flat)", PIPE_CID, "H"),
    ("PIPE_V", u"Pipes - Vertical", PIPE_CID, "V"),
    ("FITTING", u"Pipe Fittings", _bic_int("OST_PipeFitting"), None),
    ("ACCESSORY", u"Pipe Accessories", _bic_int("OST_PipeAccessory"), None),
    ("FIXTURE", u"Plumbing Fixtures", _bic_int("OST_PlumbingFixtures"), None),
]
DEFAULT_CIDS = set(it[2] for it in DEFAULT_ITEMS)

# ticked by default (first run) when found in the link
DEFAULT_LINK_CATS = _bic_ints([
    "OST_StructuralFraming", "OST_StructuralColumns", "OST_Floors",
    "OST_Walls", "OST_StructuralFoundation"])

EXCLUDE_CATS = _bic_ints([
    "OST_Rooms", "OST_Areas", "OST_MEPSpaces", "OST_HVAC_Zones", "OST_Lines",
    "OST_SketchLines", "OST_RvtLinks", "OST_Cameras", "OST_Viewers",
    "OST_ProjectBasePoint", "OST_SharedBasePoint", "OST_InternalOriginSurvey",
    "OST_IOSModelGroups", "OST_DetailComponents", "OST_Massing",
    "OST_AnalyticalNodes", "OST_ElectricalCircuit", "OST_PipingSystem",
    "OST_DuctSystem", "OST_RoomSeparationLines", "OST_MEPSpaceSeparationLines",
    "OST_AreaSchemeLines", "OST_PipeCurvesCenterLine", "OST_DuctCurvesCenterLine",
    "OST_PipeFittingCenterLine", "OST_DuctFittingCenterLine",
    "OST_CableTrayCenterLine", "OST_ConduitCenterLine", "OST_PlaceHolderPipes",
    "OST_PlaceHolderDucts", "OST_Topography", "OST_Site"])


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def eid_val(eid):
    try:
        return int(eid.Value)          # Revit 2024+
    except AttributeError:
        return int(eid.IntegerValue)   # older Revit


def new_id_list():
    id_list_type = List[ElementId]
    return id_list_type()


def make_eid(v):
    try:
        return ElementId(System.Int64(int(v)))
    except Exception:
        return ElementId(System.Int32(int(v)))


def get_name(e):
    if e is None:
        return ""
    try:
        return Element.Name.GetValue(e)
    except Exception:
        try:
            return e.Name
        except Exception:
            return ""


def get_type_label(d, e):
    try:
        t = d.GetElement(e.GetTypeId())
    except Exception:
        t = None
    if t is None:
        return get_name(e)
    fam = ""
    try:
        fam = t.FamilyName
    except Exception:
        pass
    nm = get_name(t)
    return u"{} : {}".format(fam, nm) if fam else nm


def get_system_name(e):
    try:
        p = e.get_Parameter(BuiltInParameter.RBS_SYSTEM_NAME_PARAM)
        if p is not None:
            return p.AsString() or ""
    except Exception:
        pass
    return ""


def is_placeholder(el):
    try:
        return el.GetType().Name == "PipePlaceholder"
    except Exception:
        return False


def pipe_dir(el):
    """'V' = vertical pipe, 'H' = horizontal pipe (flat or sloped)."""
    try:
        crv = el.Location.Curve
        d = crv.GetEndPoint(1).Subtract(crv.GetEndPoint(0))
        ln = d.GetLength()
    except Exception:
        return "H"
    if ln < 1e-9:
        return "H"
    return "V" if abs(d.Z) / ln >= _VERT_SIN else "H"


def count_in_category(d, cid):
    try:
        return (FilteredElementCollector(d).OfCategoryId(make_eid(cid))
                .WhereElementIsNotElementType().GetElementCount())
    except Exception:
        return 0


def make_options():
    o = Options()
    o.DetailLevel = ViewDetailLevel.Fine
    o.ComputeReferences = False
    o.IncludeNonVisibleObjects = False
    return o


def _collect_solids(geom, out):
    for g in geom:
        if isinstance(g, Solid):
            try:
                if g.Volume > 1e-9 and g.Faces.Size > 0:
                    out.append(g)
            except Exception:
                pass
        elif isinstance(g, GeometryInstance):
            try:
                sub = g.GetInstanceGeometry()
            except Exception:
                sub = None
            if sub is not None:
                _collect_solids(sub, out)


def get_solids(e, opts):
    out = []
    try:
        geom = e.get_Geometry(opts)
    except Exception:
        return out
    if geom is not None:
        _collect_solids(geom, out)
    return out


def bbox_corners(bb):
    tr = bb.Transform
    mn, mx = bb.Min, bb.Max
    pts = []
    for x in (mn.X, mx.X):
        for y in (mn.Y, mx.Y):
            for z in (mn.Z, mx.Z):
                pts.append(tr.OfPoint(XYZ(x, y, z)))
    return pts


def minmax(pts):
    xs = [p.X for p in pts]
    ys = [p.Y for p in pts]
    zs = [p.Z for p in pts]
    return XYZ(min(xs), min(ys), min(zs)), XYZ(max(xs), max(ys), max(zs))


def doevents():
    """Let WPF repaint progress bar / handle Cancel click."""
    try:
        Dispatcher.CurrentDispatcher.Invoke(
            DispatcherPriority.Background, System.Action(lambda: None))
    except Exception:
        pass


def doc_model_categories(d):
    """{cat_id_int: Category} - top-level model categories of a document."""
    res = {}
    for c in d.Settings.Categories:
        try:
            if c.CategoryType != CategoryType.Model:
                continue
        except Exception:
            continue
        cid = eid_val(c.Id)
        if cid in EXCLUDE_CATS:
            continue
        res[cid] = c
    return res


# ----------------------------------------------------------------------------
# Settings (remember last run)
#   {"global": {...}, "projects": {"<model path or title>": {...}}}
#   global  : settings used when this model has never been run before
#   project : settings of the last run in THIS model (links differ per project)
# ----------------------------------------------------------------------------
def doc_key(d):
    try:
        if d.IsWorkshared:
            from Autodesk.Revit.DB import ModelPathUtils
            p = ModelPathUtils.ConvertModelPathToUserVisiblePath(d.GetWorksharingCentralModelPath())
            if p:
                return p
    except Exception:
        pass
    return d.PathName or d.Title


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}
    cfg.setdefault("global", {})
    cfg.setdefault("projects", {})
    return cfg


def save_config(cfg):
    try:
        folder = os.path.dirname(CONFIG_FILE)
        if not os.path.exists(folder):
            os.makedirs(folder)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=1)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Window  (MODAL -> Revit locked while open, results go to pyRevit output)
# ----------------------------------------------------------------------------
class ClashWindow(forms.WPFWindow):

    def __init__(self):
        forms.WPFWindow.__init__(self, XAML_FILE)
        self.cancel_flag = False
        self.busy = False
        self.clashes = []
        self.scan_done = False
        self.scan_info = {}
        self.active_items = []      # dict: key,label,cid,dir,custom,count,cb,row
        self.link_boxes = []        # (CheckBox, RevitLinkInstance)
        self.linkcat_boxes = []     # (CheckBox, category_name)
        self.linkcat_cache = {}     # link instance id -> {cat_name: (cat_id, count)}
        self.add_cat_cache = None   # {cid: (name, count)} - active doc, lazy
        self.add_opts = []          # [(name, cid, count)] shown in combobox

        self.cfg = load_config()
        self.key = doc_key(doc)
        proj = self.cfg["projects"].get(self.key)
        # settings of this model, otherwise the last settings used anywhere
        self.st = dict(proj) if proj else dict(self.cfg["global"])
        self.saved_linkcats = self.st.get("link_cats")    # None = first run

        self._build_active_panel()
        self._build_link_panel()
        self._apply_general_settings()
        if self._checked_links():
            self._safe(self.refresh_link_categories)
        else:
            self._show_linkcat_hint()

        self.btn_act_all.Click += lambda s, e: self._set_all_active(True)
        self.btn_act_none.Click += lambda s, e: self._set_all_active(False)
        self.cb_add_cat.DropDownOpened += self.on_add_dropdown
        self.btn_add_cat.Click += self.on_add_cat
        self.btn_links_all.Click += lambda s, e: self._set_all_links(True)
        self.btn_links_none.Click += lambda s, e: self._set_all_links(False)
        self.btn_lc_all.Click += lambda s, e: self._set_all_linkcats(True)
        self.btn_lc_none.Click += lambda s, e: self._set_all_linkcats(False)
        self.btn_scan.Click += self.on_scan
        self.btn_cancel.Click += self.on_cancel
        self.btn_reset.Click += self.on_reset
        self.btn_close.Click += lambda s, e: self.Close()
        self.Closing += self.on_closing

        if proj:
            self.set_status(u"Settings of last run in this model restored.")
        elif self.st:
            self.set_status(u"First run in this model - last used settings applied.")

    # ======================= SETTINGS =======================
    def _apply_general_settings(self):
        sc = self.st.get("scope", "model")
        self.rb_scope_view.IsChecked = (sc == "view")
        self.rb_scope_sel.IsChecked = (sc == "sel")
        self.rb_scope_model.IsChecked = (sc not in ("view", "sel"))
        if "min_cm3" in self.st:
            self.tb_minvol.Text = str(self.st.get("min_cm3"))

    def _collect_settings(self):
        if bool(self.rb_scope_view.IsChecked):
            sc = "view"
        elif bool(self.rb_scope_sel.IsChecked):
            sc = "sel"
        else:
            sc = "model"
        # link category ticks: keep ticks of categories not shown now
        lc = dict(self.saved_linkcats or {})
        for cb, nm in self.linkcat_boxes:
            lc[nm] = bool(cb.IsChecked)
        return {
            "custom_cats": [it["cid"] for it in self.active_items if it["custom"]],
            "active_checked": dict((it["key"], bool(it["cb"].IsChecked))
                                   for it in self.active_items),
            "scope": sc,
            "min_cm3": self.tb_minvol.Text.strip() or "1",
            "links": [li.Name for cb, li in self.link_boxes if cb.IsChecked],
            "link_cats": lc,
        }

    def save_settings(self):
        st = self._collect_settings()
        self.cfg["projects"][self.key] = st
        g = dict(st)
        g.pop("links", None)            # link names only make sense per model
        self.cfg["global"] = g
        save_config(self.cfg)

    def on_closing(self, sender, args):
        if self.busy:                   # do not close in the middle of a scan
            self.cancel_flag = True
            args.Cancel = True
            return
        self.save_settings()

    def on_reset(self, sender, args):
        if not forms.alert("Reset all saved settings of this tool to default?",
                           yes=True, no=True):
            return
        self.cfg = {"global": {}, "projects": {}}
        save_config(self.cfg)
        self.st = {}
        self.saved_linkcats = None
        self.sp_active.Children.Clear()
        self.active_items = []
        self._build_active_panel()
        self._refresh_add_combo()
        for cb, li in self.link_boxes:
            cb.IsChecked = False
        self._apply_general_settings()
        self.tb_minvol.Text = "1"
        self._show_linkcat_hint()
        self.set_status("Settings reset to default.")

    # ======================= ACTIVE PANEL =======================
    def _build_active_panel(self):
        checked = self.st.get("active_checked", {})
        n_h = n_v = 0
        for el in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_PipeCurves)
                   .WhereElementIsNotElementType()):
            if is_placeholder(el):
                continue
            if pipe_dir(el) == "V":
                n_v += 1
            else:
                n_h += 1

        for key, label, cid, d in DEFAULT_ITEMS:
            if cid is None:
                continue
            if key == "PIPE_H":
                n = n_h
            elif key == "PIPE_V":
                n = n_v
            else:
                n = count_in_category(doc, cid)
            self._add_active_row({"key": key, "label": label, "cid": cid,
                                  "dir": d, "custom": False, "count": n},
                                 checked.get(key, True))

        cats = doc_model_categories(doc)
        for cid in self.st.get("custom_cats", []):
            c = cats.get(cid)
            if c is None or cid in DEFAULT_CIDS:
                continue
            key = "C{}".format(cid)
            self._add_active_row({"key": key, "label": c.Name, "cid": cid,
                                  "dir": None, "custom": True,
                                  "count": count_in_category(doc, cid)},
                                 checked.get(key, True))

    def _add_active_row(self, item, is_checked=True):
        cb = CheckBox()
        cb.Content = u"{}  ({})".format(item["label"], item["count"])
        cb.IsChecked = bool(is_checked)
        cb.VerticalAlignment = VerticalAlignment.Center
        item["cb"] = cb
        if item["custom"]:
            dp = DockPanel()
            dp.Margin = Thickness(0, 1, 0, 1)
            btn = Button()
            btn.Content = u"\u2715"
            btn.Width = 22
            btn.Padding = Thickness(0)
            btn.Margin = Thickness(6, 0, 0, 0)
            btn.ToolTip = "Remove this category from the list"
            btn.Click += self._make_remove(item)
            DockPanel.SetDock(btn, Dock.Right)
            dp.Children.Add(btn)
            dp.Children.Add(cb)
            item["row"] = dp
        else:
            cb.Margin = Thickness(0, 1, 0, 1)
            item["row"] = cb
        self.sp_active.Children.Add(item["row"])
        self.active_items.append(item)

    def _make_remove(self, item):
        def handler(sender, args):
            self.sp_active.Children.Remove(item["row"])
            if item in self.active_items:
                self.active_items.remove(item)
            self._refresh_add_combo()
            self.set_status(u"Removed category: {}".format(item["label"]))
        return handler

    def _used_cids(self):
        return set(it["cid"] for it in self.active_items)

    def on_add_dropdown(self, sender, args):
        if self.add_cat_cache is None:
            self.set_status("Reading model categories of active model ...")
            doevents()
            self._safe(self._load_add_cats)
            return
        self._refresh_add_combo()

    def _load_add_cats(self):
        self.Cursor = Cursors.Wait
        try:
            cache = {}
            for cid, c in doc_model_categories(doc).items():
                n = count_in_category(doc, cid)
                if n > 0:
                    cache[cid] = (c.Name, n)
            self.add_cat_cache = cache
        finally:
            self.Cursor = None
        self.set_status(u"{} model categories with elements found.".format(len(cache)))
        self._refresh_add_combo()

    def _refresh_add_combo(self):
        if self.add_cat_cache is None:
            return
        used = self._used_cids()
        self.add_opts = sorted(
            [(nm, cid, n) for cid, (nm, n) in self.add_cat_cache.items() if cid not in used],
            key=lambda r: r[0].lower())
        self.cb_add_cat.Items.Clear()
        for nm, cid, n in self.add_opts:
            self.cb_add_cat.Items.Add(u"{}  ({})".format(nm, n))

    def on_add_cat(self, sender, args):
        idx = self.cb_add_cat.SelectedIndex
        if idx < 0 or idx >= len(self.add_opts):
            forms.alert("Open the 'Add category' list and pick a category first.")
            return
        nm, cid, n = self.add_opts[idx]
        if cid in self._used_cids():
            return
        self._add_active_row({"key": "C{}".format(cid), "label": nm, "cid": cid,
                              "dir": None, "custom": True, "count": n})
        self._refresh_add_combo()
        self.set_status(u"Added category: {}".format(nm))

    def _set_all_active(self, state):
        for it in self.active_items:
            it["cb"].IsChecked = state

    def _checked_active_items(self):
        return [it for it in self.active_items if it["cb"].IsChecked]

    # ======================= LINK PANEL =======================
    def _build_link_panel(self):
        saved = set(self.st.get("links", []))
        for li in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            ldoc = li.GetLinkDocument()
            cb = CheckBox()
            if ldoc is None:
                cb.Content = u"{}  (not loaded)".format(li.Name)
                cb.IsEnabled = False
                cb.IsChecked = False
            else:
                cb.Content = li.Name
                cb.IsChecked = li.Name in saved
            cb.Margin = Thickness(0, 1, 0, 1)
            cb.Click += self._on_link_click
            self.sp_links.Children.Add(cb)
            self.link_boxes.append((cb, li))

    def _on_link_click(self, sender, args):
        self._safe(self.refresh_link_categories)

    def _safe(self, fn):
        try:
            fn()
        except Exception:
            import traceback
            self.Cursor = None
            forms.alert(u"Error:\n{}".format(traceback.format_exc()), title="Clash Check")

    def _show_linkcat_hint(self):
        self.sp_linkcats.Children.Clear()
        self.linkcat_boxes = []
        hint = TextBlock()
        hint.Text = "Tick a link model to load its categories."
        hint.Foreground = Brushes.Gray
        hint.TextWrapping = TextWrapping.Wrap
        self.sp_linkcats.Children.Add(hint)

    def _set_all_links(self, state):
        for cb, li in self.link_boxes:
            if cb.IsEnabled:
                cb.IsChecked = state
        self._safe(self.refresh_link_categories)

    def _checked_links(self):
        out = []
        for cb, li in self.link_boxes:
            if not (cb.IsEnabled and cb.IsChecked):
                continue
            try:
                if li.GetLinkDocument() is not None:
                    out.append(li)
            except Exception:
                pass
        return out

    def _get_link_cats(self, li):
        """{cat_name: (cat_id_int, count)} of physical model categories in link."""
        key = eid_val(li.Id)
        cached = self.linkcat_cache.get(key)
        if cached is not None:
            return cached
        res = {}
        ldoc = li.GetLinkDocument()
        if ldoc is None:
            return res
        self.set_status(u"Reading categories of link: {} ...".format(li.Name))
        doevents()
        for cid, c in doc_model_categories(ldoc).items():
            n = count_in_category(ldoc, cid)
            if n > 0:
                res[c.Name] = (cid, n)
        self.linkcat_cache[key] = res
        return res

    def refresh_link_categories(self):
        # current ticks have priority, then saved ticks, then defaults
        prev = dict((nm, bool(cb.IsChecked)) for cb, nm in self.linkcat_boxes)
        if self.linkcat_boxes:
            if self.saved_linkcats is None:
                self.saved_linkcats = {}
            self.saved_linkcats.update(prev)
        checked = self._checked_links()
        if len(checked) == 0:
            self._show_linkcat_hint()
            self.set_status("No link checked.")
            return
        merged = {}
        self.Cursor = Cursors.Wait
        try:
            for li in checked:
                for nm, (cid, n) in self._get_link_cats(li).items():
                    if nm in merged:
                        merged.get(nm)[1] += n
                    else:
                        merged[nm] = [cid, n]
        finally:
            self.Cursor = None
        if not merged:
            self._show_linkcat_hint()
            self.set_status("Checked link(s) contain no physical model elements.")
            return
        saved = self.saved_linkcats or {}
        self.sp_linkcats.Children.Clear()
        self.linkcat_boxes = []
        for nm in sorted(merged.keys()):
            cid, n = merged[nm]
            cb = CheckBox()
            cb.Content = u"{}  ({})".format(nm, n)
            if nm in prev:
                cb.IsChecked = prev.get(nm)
            elif nm in saved:
                cb.IsChecked = bool(saved.get(nm))
            else:
                cb.IsChecked = (cid in DEFAULT_LINK_CATS)
            cb.Margin = Thickness(0, 1, 0, 1)
            self.sp_linkcats.Children.Add(cb)
            self.linkcat_boxes.append((cb, nm))
        self.set_status(u"{} link(s) checked, {} categories found.".format(
            len(checked), len(merged)))

    def _set_all_linkcats(self, state):
        for cb, nm in self.linkcat_boxes:
            cb.IsChecked = state

    # ======================= SCAN =======================
    def set_status(self, text):
        self.txt_status.Text = text

    def on_cancel(self, sender, args):
        self.cancel_flag = True
        self.set_status("Cancelling...")

    def _collect_active_elements(self, items):
        by_cid = {}
        for it in items:
            by_cid.setdefault(it["cid"], set()).add(it["dir"])

        def accept(el):
            if el is None or el.Category is None:
                return False
            if is_placeholder(el) or isinstance(el, RevitLinkInstance):
                return False
            dirs = by_cid.get(eid_val(el.Category.Id))
            if dirs is None:
                return False
            if None in dirs:
                return True
            return pipe_dir(el) in dirs

        elems = []
        if bool(self.rb_scope_sel.IsChecked):
            for eid in uidoc.Selection.GetElementIds():
                el = doc.GetElement(eid)
                if accept(el):
                    elems.append(el)
            return elems

        for cid in by_cid.keys():
            if bool(self.rb_scope_view.IsChecked):
                col = FilteredElementCollector(doc, uidoc.ActiveView.Id)
            else:
                col = FilteredElementCollector(doc)
            for el in col.OfCategoryId(make_eid(cid)).WhereElementIsNotElementType():
                if accept(el):
                    elems.append(el)
        return elems

    def _active_group_label(self, el):
        cid = eid_val(el.Category.Id) if el.Category else None
        if cid == PIPE_CID:
            return "Pipes - Vertical" if pipe_dir(el) == "V" else "Pipes - Horizontal"
        return el.Category.Name if el.Category else ""

    def on_scan(self, sender, args):
        if self.busy:
            return
        self.save_settings()
        self.busy = True
        self.cancel_flag = False
        self.btn_scan.IsEnabled = False
        self.btn_cancel.IsEnabled = True
        try:
            self.run_scan()
        except Exception as ex:
            import traceback
            forms.alert(u"Scan error:\n{}".format(traceback.format_exc()), title="Clash Check")
            self.set_status(u"Error: {}".format(ex))
        finally:
            self.busy = False
            self.btn_scan.IsEnabled = True
            self.btn_cancel.IsEnabled = False
        if self.scan_done:
            self.Close()

    def run_scan(self):
        """Scan inside the modal window, then close it and print the results."""
        items = self._checked_active_items()
        if not items:
            forms.alert("Check at least one category in section 1.")
            return
        sel_cat_names = set(nm for cb, nm in self.linkcat_boxes if cb.IsChecked)
        if not sel_cat_names:
            forms.alert("Check at least one link category.")
            return

        links = []
        for li in self._checked_links():
            cats = self._get_link_cats(li)
            ids = new_id_list()
            for nm in sel_cat_names:
                if nm in cats:
                    ids.Add(make_eid(cats.get(nm)[0]))
            if ids.Count == 0:
                continue
            tr = li.GetTotalTransform()
            links.append({"doc": li.GetLinkDocument(), "tr": tr, "inv": tr.Inverse,
                          "name": li.Name, "filter": ElementMulticategoryFilter(ids)})
        if not links:
            forms.alert("No checked link contains the checked categories.")
            return

        try:
            min_cm3 = max(0.0, float(self.tb_minvol.Text.replace(",", ".")))
        except Exception:
            min_cm3 = 1.0
        use_bool = min_cm3 > 0
        min_ft3 = min_cm3 * FT3_PER_CM3

        self.set_status("Collecting active elements ...")
        doevents()
        elems = self._collect_active_elements(items)
        if not elems:
            forms.alert("No element to scan with the current ticks / scope.")
            return

        self.clashes = []
        total = len(elems)
        self.pb.Maximum = total
        self.pb.Value = 0
        opts = make_options()
        cancelled = False

        for i, el in enumerate(elems):
            if i % 5 == 0:
                self.pb.Value = i
                self.set_status(u"Scanning {}/{}  -  clashes found: {}".format(
                    i + 1, total, len(self.clashes)))
                doevents()
            if self.cancel_flag:
                cancelled = True
                break

            solids = get_solids(el, opts)
            if not solids:
                continue
            grp = None

            for ld in links:
                hits = {}
                lsolid_cache = {}
                for s in solids:
                    try:
                        ts = SolidUtils.CreateTransformed(s, ld["inv"])
                        mn, mx = minmax(bbox_corners(ts.GetBoundingBox()))
                        found = list(FilteredElementCollector(ld["doc"])
                                     .WherePasses(ld["filter"])
                                     .WherePasses(BoundingBoxIntersectsFilter(Outline(mn, mx)))
                                     .WherePasses(ElementIntersectsSolidFilter(ts))
                                     .ToElements())
                    except Exception:
                        continue
                    for le in found:
                        k = eid_val(le.Id)
                        h = hits.get(k)
                        if h is None:
                            h = {"el": le, "vol": 0.0, "unknown": False}
                            hits[k] = h
                        if not use_bool:
                            continue
                        lsol = lsolid_cache.get(k)
                        if lsol is None:
                            lsol = get_solids(le, opts)
                            lsolid_cache.setdefault(k, lsol)
                        if not lsol:
                            h["unknown"] = True
                            continue
                        for ls in lsol:
                            try:
                                inter = BooleanOperationsUtils.ExecuteBooleanOperation(
                                    ts, ls, BooleanOperationsType.Intersect)
                                if inter.Volume > 0:
                                    h["vol"] += inter.Volume
                            except Exception:
                                h["unknown"] = True

                for k, h in hits.items():
                    if use_bool and (not h["unknown"]) and h["vol"] < min_ft3:
                        continue   # only touching
                    if grp is None:
                        grp = self._active_group_label(el)
                    le = h["el"]
                    self.clashes.append({
                        "no": len(self.clashes) + 1,
                        "aid": eid_val(el.Id),
                        "ActiveCategory": grp,
                        "ActiveType": get_type_label(doc, el),
                        "System": get_system_name(el),
                        "LinkModel": ld["name"],
                        "LinkID": str(eid_val(le.Id)),
                        "LinkCategory": le.Category.Name if le.Category else "",
                        "LinkType": get_type_label(ld["doc"], le),
                        "Overlap_cm3": round(h["vol"] / FT3_PER_CM3, 2) if use_bool else 0.0,
                    })

        self.scan_info = {"total": total, "cancelled": cancelled,
                          "min_cm3": min_cm3, "links": [ld["name"] for ld in links],
                          "cats": [it["label"] for it in items],
                          "linkcats": sorted(sel_cat_names)}
        self.scan_done = True


# ----------------------------------------------------------------------------
# Output (pyRevit output window - non modal, IDs are clickable)
# ----------------------------------------------------------------------------
def print_results(clashes, info):
    output = script.get_output()
    output.set_title("Clash Check - Active vs Link")
    output.print_md("# Clash Check - Active model vs Link")
    output.print_md(
        u"**Model:** {}  \n**Scanned:** {} element(s){}  \n**Min overlap:** {} cm3  \n"
        u"**Links:** {}  \n**Active categories:** {}  \n**Link categories:** {}".format(
            doc.Title, info.get("total", 0),
            " (CANCELLED)" if info.get("cancelled") else "",
            info.get("min_cm3", 0),
            ", ".join(info.get("links", [])),
            ", ".join(info.get("cats", [])),
            ", ".join(info.get("linkcats", []))))

    if not clashes:
        output.print_md("## No clash found.")
        return

    aids = []
    for r in clashes:
        if r["aid"] not in aids:
            aids.append(r["aid"])
    output.print_md(u"## {} clash(es) on {} active element(s)".format(len(clashes), len(aids)))
    output.print_md(u"Select all clashing active elements: {}".format(
        output.linkify([make_eid(a) for a in aids], title="{} elements".format(len(aids)))))

    groups = {}
    order = []
    for r in clashes:
        g = r["ActiveCategory"]
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(r)

    for g in order:
        rows = []
        for r in groups[g]:
            rows.append([
                r["no"],
                output.linkify(make_eid(r["aid"])),
                r["ActiveType"],
                r["System"],
                r["LinkModel"],
                r["LinkID"],
                r["LinkCategory"],
                r["LinkType"],
                r["Overlap_cm3"],
            ])
        output.print_table(
            table_data=rows,
            title=u"{}  ({} clash(es))".format(g, len(rows)),
            columns=["No", "Active ID", "Active Type", "System", "Link Model",
                     "Link ID", "Link Category", "Link Type", "Overlap cm3"])

    output.print_md("### Copy lists")
    output.print_md(u"**Active IDs:** {}".format(", ".join(str(a) for a in aids)))
    lids = []
    for r in clashes:
        if r["LinkID"] not in lids:
            lids.append(r["LinkID"])
    output.print_md(u"**Link IDs:** {}".format(", ".join(lids)))


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
if not os.path.exists(XAML_FILE):
    forms.alert(u"UI file not found:\n{}\n\nPut ui.xaml in the same folder as script.py."
                .format(XAML_FILE), title="Clash Check")
else:
    win = ClashWindow()
    win.ShowDialog()          # MODAL
    if win.scan_done:
        print_results(win.clashes, win.scan_info)

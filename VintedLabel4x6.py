"""
Vinted Label 4x6 - turns a full-page shipping label PDF (Letter / A4) into a
4x6 label and prints it on a thermal label printer (iDPRT SP410, etc.).
Runs on Windows, macOS and Linux.

  VintedLabel4x6                       -> open the app, drop a label PDF on it
  VintedLabel4x6 label.pdf             -> open the app with that label loaded
  VintedLabel4x6 label.pdf --auto      -> save <name>_4x6.pdf, no window
  VintedLabel4x6 label.pdf --print     -> print to your saved printer, no window
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject

APP_NAME = "Vinted Label 4x6"
UI_FONT = "Helvetica"  # replaced with the theme font once the window exists
ACCENT, ACCENT_HOVER = "#007782", "#005f68"   # Vinted teal
BOX_COLOR = "#e5484d"

LABEL_W, LABEL_H = 4 * 72, 6 * 72   # 4x6 inches in PDF points
MARGIN = 0.08 * 72                  # small safety margin inside the label
DETECT_SCALE = 2.0                  # render at 144 dpi for auto-detection

IS_WIN, IS_MAC = sys.platform.startswith("win"), sys.platform == "darwin"


def _settings_dir():
    if IS_WIN:
        return Path(os.environ.get("APPDATA", Path.home())) / "VintedLabel4x6"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "VintedLabel4x6"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "VintedLabel4x6"


SETTINGS_FILE = _settings_dir() / "settings.json"
_PDFIUM_LOCK = threading.Lock()   # pdfium isn't thread-safe


# ============================================================ settings

def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(**changes):
    s = load_settings()
    s.update(changes)
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def resource(name):
    """Path to a file bundled inside the app, or in ./assets when run from source."""
    for base in (Path(getattr(sys, "_MEIPASS", "")), Path(__file__).parent / "assets"):
        if (base / name).exists():
            return base / name
    return None


# ============================================================ PDF logic

def normalize_pdf(path):
    """Bake any page /Rotate into the content so coordinates are simple."""
    reader = PdfReader(str(path))
    writer = PdfWriter()
    for page in reader.pages:
        if page.get("/Rotate", 0):
            page.transfer_rotation_to_content()
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def page_count(pdf_bytes):
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def page_box(pdf_bytes, index):
    """(left, bottom, right, top) of a page's visible area."""
    cb = PdfReader(io.BytesIO(pdf_bytes)).pages[index].cropbox
    return tuple(float(v) for v in (cb.left, cb.bottom, cb.right, cb.top))


def render_page(pdf_bytes, index, scale, **opts):
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            page = doc[index]
            img = page.render(scale=scale, **opts).to_pil()
            page.close()
            return img
        finally:
            doc.close()


def _runs(mask_1d, min_gap):
    """Split a 1-D 'has ink' array into (start, end) runs, merging runs
    separated by gaps shorter than min_gap."""
    runs, start, gap, end = [], None, 0, 0
    for i, v in enumerate(mask_1d):
        if v:
            if start is None:
                start = i
            gap, end = 0, i
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                runs.append((start, end))
                start, gap = None, 0
    if start is not None:
        runs.append((start, end))
    return runs


def detect_label_box(pil_img):
    """Find the label on a rendered page -> (x0, y0, x1, y1) in pixels.
    Splits the page into blocks of ink separated by big blank bands and keeps
    the largest one, which skips instructions / notes printed away from the label."""
    gray = pil_img.convert("L")
    w, h = gray.size
    px = gray.load()
    step = 2
    ink = [[px[x, y] < 200 for x in range(0, w, step)] for y in range(0, h, step)]
    gap = int(0.3 * 72 * DETECT_SCALE / step)  # ~0.3 inch of white = separator

    def best_block(y0, y1, x0, x1):
        rows = [any(ink[y][x0:x1]) for y in range(y0, y1)]
        best = None
        for ry0, ry1 in _runs(rows, gap):
            ry0 += y0; ry1 += y0
            cols = [any(ink[y][x] for y in range(ry0, ry1 + 1)) for x in range(x0, x1)]
            for cx0, cx1 in _runs(cols, gap):
                cx0 += x0; cx1 += x0
                area = (ry1 - ry0) * (cx1 - cx0)
                if best is None or area > best[0]:
                    best = (area, cx0, ry0, cx1, ry1)
        return best

    b = best_block(0, len(ink), 0, len(ink[0]))
    if not b:
        return (0, 0, w, h)
    _, x0, y0, x1, y1 = b
    b2 = best_block(y0, y1 + 1, x0, x1 + 1)
    if b2:
        _, x0, y0, x1, y1 = b2
    pad = int(4 * DETECT_SCALE / step)
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(len(ink[0]) - 1, x1 + pad), min(len(ink) - 1, y1 + pad)
    return (x0 * step, y0 * step, (x1 + 1) * step, (y1 + 1) * step)


def pixel_box_to_pdf(box_px, pbox, scale):
    """Pixel box (top-left origin) -> PDF coords (bottom-left origin)."""
    left, _, _, top = pbox
    x0, y0, x1, y1 = box_px
    return (left + x0 / scale, top - y1 / scale, left + x1 / scale, top - y0 / scale)


def auto_boxes(pdf_bytes):
    boxes = []
    for i in range(page_count(pdf_bytes)):
        img = render_page(pdf_bytes, i, DETECT_SCALE)
        boxes.append(pixel_box_to_pdf(detect_label_box(img), page_box(pdf_bytes, i), DETECT_SCALE))
    return boxes


def build_4x6(pdf_bytes, boxes, rotate="auto"):
    """Crop each page to its box and fit it onto a 4x6 portrait page.
    rotate: 'auto' (rotate landscape crops), 0, 90 or 270. Returns PDF bytes."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page, (x0, y0, x1, y1) in zip(reader.pages, boxes):
        bw, bh = x1 - x0, y1 - y0
        # shrink the page to the box; pypdf clips to it when merging
        page.mediabox = RectangleObject([x0, y0, x1, y1])
        page.cropbox = RectangleObject([x0, y0, x1, y1])

        rot = rotate
        if rot == "auto":
            rot = 90 if bw > bh else 0
        rw, rh = (bh, bw) if rot in (90, 270) else (bw, bh)

        s = min((LABEL_W - 2 * MARGIN) / rw, (LABEL_H - 2 * MARGIN) / rh)
        t = Transformation().translate(-x0, -y0)
        if rot == 90:
            t = t.rotate(90).translate(bh, 0)
        elif rot == 270:
            t = t.rotate(270).translate(0, bw)
        t = t.scale(s, s).translate((LABEL_W - rw * s) / 2, (LABEL_H - rh * s) / 2)

        new_page = writer.add_blank_page(LABEL_W, LABEL_H)
        new_page.merge_transformed_page(page, t)
        new_page.mediabox = RectangleObject([0, 0, LABEL_W, LABEL_H])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def default_out(path):
    p = Path(path)
    return p.with_name(p.stem + "_4x6.pdf")


def open_file(path):
    try:
        if IS_WIN:
            os.startfile(str(path))
        else:
            subprocess.Popen(["open" if IS_MAC else "xdg-open", str(path)])
    except Exception:
        pass


# ============================================================ printing
# Windows: draws straight to the printer with GDI (pywin32).
# macOS / Linux: hands the 4x6 PDF to CUPS with `lp`.

def printing_available():
    if IS_WIN:
        try:
            import win32print  # noqa: F401
            return True
        except Exception:
            return False
    return shutil.which("lp") is not None


def list_printers():
    """-> (printer names, system default or None)"""
    if IS_WIN:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        names = [p[2] for p in win32print.EnumPrinters(flags)]
        try:
            default = win32print.GetDefaultPrinter()
        except Exception:
            default = None
        return names, default

    def run(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return ""
    names = [l.strip() for l in run("lpstat", "-e").splitlines() if l.strip()]
    if not names:  # older CUPS
        names = [l.split()[1] for l in run("lpstat", "-p").splitlines() if l.startswith("printer ")]
    d = run("lpstat", "-d")
    default = d.split(":", 1)[1].strip() if ":" in d else None
    return names, default


def pick_printer(names, default):
    saved = load_settings().get("printer")
    if saved in names:
        return saved
    for n in names:
        if any(k in n.lower() for k in ("sp410", "idprt", "label", "4x6", "thermal")):
            return n
    return default if default in names else (names[0] if names else None)


def print_label(label_pdf, printer, copies=1):
    if IS_WIN:
        _print_windows(label_pdf, printer, copies)
    else:
        _print_cups(label_pdf, printer, copies)


def _print_cups(label_pdf, printer, copies):
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(label_pdf)
        r = subprocess.run(["lp", "-d", printer, "-n", str(max(1, copies)),
                            "-o", "media=Custom.4x6in", "-o", "fit-to-page", tmp],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "lp failed")
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def render_for_print(label_pdf, dpi):
    """Render every 4x6 page as crisp black & white at the printer's resolution."""
    images = []
    for i in range(page_count(label_pdf)):
        img = render_page(label_pdf, i, dpi / 72, grayscale=True,
                          no_smoothtext=True, no_smoothimage=True, no_smoothpath=True)
        img = img.convert("L").point(lambda v: 255 if v > 150 else 0).convert("RGB")
        images.append(img)
    return images


def printer_dc(printer):
    """Open a device context for the printer, asking the driver for 4x6 portrait."""
    import win32con, win32gui, win32print, win32ui
    devmode = None
    try:
        h = win32print.OpenPrinter(printer)
        try:
            devmode = win32print.GetPrinter(h, 2)["pDevMode"]
        finally:
            win32print.ClosePrinter(h)
    except Exception:
        pass
    if devmode is not None:
        devmode.Orientation = win32con.DMORIENT_PORTRAIT
        devmode.PaperWidth = 1016    # tenths of a mm -> 4 in
        devmode.PaperLength = 1524   # 6 in
        devmode.Fields |= (win32con.DM_ORIENTATION | win32con.DM_PAPERWIDTH
                           | win32con.DM_PAPERLENGTH)
        hdc = win32gui.CreateDC("WINSPOOL", printer, devmode)
        return win32ui.CreateDCFromHandle(hdc)
    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    return dc


def _print_windows(label_pdf, printer, copies=1):
    import win32con
    from PIL import ImageWin
    dc = printer_dc(printer)
    try:
        dpi = dc.GetDeviceCaps(win32con.LOGPIXELSX) or 203
        area_w = dc.GetDeviceCaps(win32con.HORZRES)
        area_h = dc.GetDeviceCaps(win32con.VERTRES)
        images = render_for_print(label_pdf, dpi)
        dc.StartDoc("Vinted label")
        for _ in range(max(1, copies)):
            for img in images:
                s = min(area_w / img.width, area_h / img.height)
                w, h = int(img.width * s), int(img.height * s)
                x, y = (area_w - w) // 2, (area_h - h) // 2
                dc.StartPage()
                ImageWin.Dib(img).draw(dc.GetHandleOutput(), (x, y, x + w, y + h))
                dc.EndPage()
        dc.EndDoc()
    finally:
        dc.DeleteDC()


# ============================================================ GUI

def run_gui(initial=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import customtkinter as ctk
    from PIL import ImageTk

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        base_classes = (ctk.CTk, TkinterDnD.DnDWrapper)
    except Exception:
        TkinterDnD = None
        base_classes = (ctk.CTk,)

    ctk.set_appearance_mode("system")
    MOD, MOD_TXT = ("Command", "⌘") if IS_MAC else ("Control", "Ctrl+")
    ROT_OPTIONS = {"Auto": "auto", "0°": 0, "90°": 90, "270°": 270}

    class App(*base_classes):
        def __init__(self):
            super().__init__()
            self.dnd = False
            if TkinterDnD:
                try:
                    self.TkdndVersion = TkinterDnD._require(self)
                    self.dnd = True
                except Exception:
                    pass

            global UI_FONT
            UI_FONT = ctk.CTkFont().cget("family")
            self.title(APP_NAME)
            self.geometry("1040x760")
            self.minsize(860, 620)
            self._set_icon()

            self.path = self.pdf = self.label_pdf = None
            self.boxes, self.auto, self.idx = [], [], 0
            self.view = None          # (scale, offset_x, offset_y, page_box) of preview
            self.drag_start = None
            self.print_result = None

            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(0, weight=1)
            self._build_preview()
            self._build_sidebar()

            self.bind(f"<{MOD}-o>", lambda e: self.pick())
            self.bind(f"<{MOD}-p>", lambda e: self.do_print())
            self.bind(f"<{MOD}-s>", lambda e: self.do_save())

            if initial:
                self.after(150, lambda: self.load(initial))

        def _set_icon(self):
            try:
                if IS_WIN and resource("icon.ico"):
                    # CTk sets its own icon shortly after start, so set ours after it
                    self.after(250, lambda: self.iconbitmap(str(resource("icon.ico"))))
                elif resource("icon.png"):
                    self._icon_img = tk.PhotoImage(file=str(resource("icon.png")))
                    self.after(250, lambda: self.iconphoto(True, self._icon_img))
            except Exception:
                pass

        # ---------- layout
        def _build_preview(self):
            wrap = ctk.CTkFrame(self, corner_radius=14)
            wrap.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=16)
            dark = ctk.get_appearance_mode() == "Dark"
            self.canvas_bg = "#1d1f22" if dark else "#e8eaed"
            self.canvas_fg = "#8b9096"
            self.canvas = tk.Canvas(wrap, bg=self.canvas_bg, highlightthickness=0, cursor="crosshair")
            self.canvas.pack(fill="both", expand=True, padx=6, pady=6)
            self.canvas.bind("<Configure>", lambda e: self._schedule_redraw())
            self.canvas.bind("<ButtonPress-1>", self._drag_begin)
            self.canvas.bind("<B1-Motion>", self._drag_move)
            self.canvas.bind("<ButtonRelease-1>", self._drag_end)
            if self.dnd:
                self.canvas.drop_target_register(DND_FILES)
                self.canvas.dnd_bind("<<Drop>>", self._on_drop)
            self._redraw_job = None

        def _section(self, parent, text):
            ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=("gray45", "gray60"), anchor="w").pack(fill="x", padx=20, pady=(18, 6))

        def _build_sidebar(self):
            side = ctk.CTkFrame(self, width=290, corner_radius=14)
            side.grid(row=0, column=1, sticky="ns", padx=(8, 16), pady=16)
            side.pack_propagate(False)

            ctk.CTkLabel(side, text=APP_NAME, font=ctk.CTkFont(size=20, weight="bold"),
                         anchor="w").pack(fill="x", padx=20, pady=(20, 0))
            ctk.CTkLabel(side, text="Full-page label → 4×6 thermal", anchor="w",
                         text_color=("gray45", "gray60")).pack(fill="x", padx=20)

            ctk.CTkButton(side, text="Open PDF…", height=36, fg_color="transparent", border_width=1,
                          text_color=("gray10", "gray90"), border_color=("gray70", "gray35"),
                          hover_color=("gray85", "gray25"), command=self.pick).pack(fill="x", padx=20, pady=(16, 4))
            self.file_lbl = ctk.CTkLabel(side, text="No file loaded", anchor="w",
                                         font=ctk.CTkFont(size=12), text_color=("gray45", "gray60"))
            self.file_lbl.pack(fill="x", padx=20)

            self._section(side, "LABEL AREA")
            row = ctk.CTkFrame(side, fg_color="transparent")
            row.pack(fill="x", padx=20)
            self.prev_b = ctk.CTkButton(row, text="‹", width=32, command=lambda: self.go(-1))
            self.prev_b.pack(side="left")
            self.page_lbl = ctk.CTkLabel(row, text="—", width=70)
            self.page_lbl.pack(side="left", padx=4)
            self.next_b = ctk.CTkButton(row, text="›", width=32, command=lambda: self.go(1))
            self.next_b.pack(side="left")
            self.auto_b = ctk.CTkButton(row, text="Reset", width=70, command=self.reset_box)
            self.auto_b.pack(side="right")
            for b in (self.prev_b, self.next_b, self.auto_b):
                b.configure(fg_color=("gray82", "gray28"), hover_color=("gray75", "gray35"),
                            text_color=("gray10", "gray90"))
            ctk.CTkLabel(side, text="Drag on the page to pick a different area", anchor="w",
                         font=ctk.CTkFont(size=11), text_color=("gray45", "gray60")).pack(fill="x", padx=20, pady=(4, 0))

            self._section(side, "ROTATION")
            self.rot = ctk.CTkSegmentedButton(side, values=list(ROT_OPTIONS), command=lambda _: self.rebuild(),
                                              selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
            self.rot.set("Auto")
            self.rot.pack(fill="x", padx=20)

            self._section(side, "4×6 RESULT")
            self.thumb = ctk.CTkLabel(side, text="", width=144, height=216, corner_radius=6,
                                      fg_color=("gray85", "gray22"))
            self.thumb.pack(padx=20)

            bottom = ctk.CTkFrame(side, fg_color="transparent")
            bottom.pack(side="bottom", fill="x", padx=20, pady=20)

            self.printers = []
            if printing_available():
                try:
                    names, default = list_printers()
                    self.printers = names
                    chosen = pick_printer(names, default)
                    if not names:
                        self.after(500, lambda: self.set_status("No printers found — you can still save as PDF."))
                except Exception:
                    chosen = None
            else:
                chosen = None
            self.printer_menu = ctk.CTkOptionMenu(bottom, values=self.printers or ["No printers found"],
                                                  command=lambda n: save_settings(printer=n),
                                                  fg_color=("gray82", "gray28"), button_color=("gray75", "gray35"),
                                                  button_hover_color=("gray70", "gray40"),
                                                  text_color=("gray10", "gray90"), dynamic_resizing=False)
            self.printer_menu.set(chosen or "No printers found")
            self.printer_menu.pack(fill="x", pady=(0, 8))

            copies_row = ctk.CTkFrame(bottom, fg_color="transparent")
            copies_row.pack(fill="x", pady=(0, 10))
            ctk.CTkLabel(copies_row, text="Copies").pack(side="left")
            self.copies = ctk.CTkSegmentedButton(copies_row, values=["1", "2", "3"],
                                                 selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
            self.copies.set("1")
            self.copies.pack(side="right")

            self.print_b = ctk.CTkButton(bottom, text="Print label", height=44, corner_radius=10,
                                         font=ctk.CTkFont(size=15, weight="bold"),
                                         fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.do_print)
            self.print_b.pack(fill="x")
            self.save_b = ctk.CTkButton(bottom, text="Save as PDF", height=34, fg_color="transparent",
                                        border_width=1, text_color=("gray10", "gray90"),
                                        border_color=("gray70", "gray35"), hover_color=("gray85", "gray25"),
                                        command=self.do_save)
            self.save_b.pack(fill="x", pady=(8, 0))
            self.status = ctk.CTkLabel(bottom, text="", font=ctk.CTkFont(size=12), wraplength=240,
                                       text_color=("gray45", "gray60"))
            self.status.pack(fill="x", pady=(10, 0))

            self._update_controls()

        # ---------- state
        def _update_controls(self):
            loaded = self.pdf is not None
            n = len(self.boxes)
            self.page_lbl.configure(text=f"Page {self.idx + 1}/{n}" if loaded else "—")
            multi = "normal" if n > 1 else "disabled"
            self.prev_b.configure(state=multi)
            self.next_b.configure(state=multi)
            self.auto_b.configure(state="normal" if loaded else "disabled")
            self.save_b.configure(state="normal" if loaded else "disabled")
            can_print = loaded and bool(self.printers)
            self.print_b.configure(state="normal" if can_print else "disabled")

        def set_status(self, text):
            self.status.configure(text=text)

        # ---------- file handling
        def pick(self):
            p = filedialog.askopenfilename(title="Open label PDF", filetypes=[("PDF files", "*.pdf")])
            if p:
                self.load(p)

        def _on_drop(self, event):  # drag & drop onto the window
            files = [f for f in self.tk.splitlist(event.data) if f.lower().endswith(".pdf")]
            if files:
                self.load(files[0])
            else:
                self.set_status("That isn't a PDF.")

        def load(self, path):
            self.set_status("Finding the label…")
            self.update_idletasks()
            try:
                self.pdf = normalize_pdf(path)
                self.auto = auto_boxes(self.pdf)
            except Exception as e:
                self.pdf = None
                self.set_status("")
                messagebox.showerror(APP_NAME, f"Couldn't open that PDF:\n\n{e}")
                return
            self.path, self.boxes, self.idx = path, list(self.auto), 0
            name = Path(path).name
            self.file_lbl.configure(text=name if len(name) < 34 else name[:31] + "…")
            self.title(f"{APP_NAME} — {name}")
            self.set_status("Ready to print.")
            self.rebuild()
            self.redraw()

        def rebuild(self):
            """Regenerate the 4x6 PDF and its thumbnail."""
            self._update_controls()
            if self.pdf is None:
                return
            self.label_pdf = build_4x6(self.pdf, self.boxes, ROT_OPTIONS[self.rot.get()])
            img = render_page(self.label_pdf, min(self.idx, page_count(self.label_pdf) - 1), 1.0)
            self.thumb_img = ctk.CTkImage(light_image=img, dark_image=img, size=(144, 216))
            self.thumb.configure(image=self.thumb_img)

        # ---------- preview canvas
        def _schedule_redraw(self):
            if self._redraw_job:
                self.after_cancel(self._redraw_job)
            self._redraw_job = self.after(120, self.redraw)

        def redraw(self):
            self._redraw_job = None
            c = self.canvas
            c.delete("all")
            cw, ch = c.winfo_width(), c.winfo_height()
            if self.pdf is None:
                self.view = None
                msg = "Drop a Vinted label PDF here" if self.dnd else "Open a Vinted label PDF"
                x, y = cw / 2, ch / 2 - 30
                c.create_rectangle(x - 22, y - 30, x + 22, y + 26, outline=self.canvas_fg, width=2)
                for i, w in enumerate((26, 18, 26)):
                    c.create_line(x - 13, y - 14 + i * 10, x - 13 + w, y - 14 + i * 10, fill=self.canvas_fg, width=2)
                c.create_text(x, y + 56, text=msg, font=(UI_FONT, 14), fill=self.canvas_fg)
                c.create_text(x, y + 80, text=f"or press {MOD_TXT}O", font=(UI_FONT, 11), fill=self.canvas_fg)
                return
            pbox = page_box(self.pdf, self.idx)
            pw, ph = pbox[2] - pbox[0], pbox[3] - pbox[1]
            scale = max(0.1, min((cw - 40) / pw, (ch - 40) / ph))
            img = render_page(self.pdf, self.idx, scale)
            ox, oy = (cw - img.width) // 2, (ch - img.height) // 2
            self.page_img = ImageTk.PhotoImage(img)
            c.create_image(ox, oy, anchor="nw", image=self.page_img)
            self.view = (scale, ox, oy, pbox, img.width, img.height)
            self.draw_box()

        def draw_box(self):
            c = self.canvas
            c.delete("box")
            if not self.view:
                return
            scale, ox, oy, (left, _, _, top), iw, ih = self.view
            x0, y0, x1, y1 = self.boxes[self.idx]
            a, b = ox + (x0 - left) * scale, oy + (top - y1) * scale
            d, e = ox + (x1 - left) * scale, oy + (top - y0) * scale
            # dim everything outside the box
            for r in ((ox, oy, ox + iw, b), (ox, e, ox + iw, oy + ih), (ox, b, a, e), (d, b, ox + iw, e)):
                if r[2] > r[0] and r[3] > r[1]:
                    c.create_rectangle(*r, fill="black", stipple="gray25", width=0, tags="box")
            c.create_rectangle(a, b, d, e, outline=BOX_COLOR, width=3, tags="box")

        def _clamp(self, x, y):
            _, ox, oy, _, iw, ih = self.view
            return min(max(x, ox), ox + iw), min(max(y, oy), oy + ih)

        def _drag_begin(self, e):
            self.drag_start = self._clamp(e.x, e.y) if self.view else None

        def _drag_move(self, e):
            if not self.drag_start:
                return
            self.canvas.delete("drag")
            self.canvas.create_rectangle(*self.drag_start, *self._clamp(e.x, e.y), outline=ACCENT,
                                         dash=(5, 3), width=2, tags="drag")

        def _drag_end(self, e):
            if not self.drag_start:
                return
            self.canvas.delete("drag")
            (ax, ay), (bx, by) = self.drag_start, self._clamp(e.x, e.y)
            self.drag_start = None
            if abs(bx - ax) < 15 or abs(by - ay) < 15:
                return  # just a click
            scale, ox, oy, pbox, _, _ = self.view
            px = (min(ax, bx) - ox, min(ay, by) - oy, max(ax, bx) - ox, max(ay, by) - oy)
            self.boxes[self.idx] = pixel_box_to_pdf(px, pbox, scale)
            self.draw_box()
            self.rebuild()

        def go(self, d):
            self.idx = (self.idx + d) % len(self.boxes)
            self.rebuild()
            self.redraw()

        def reset_box(self):
            if self.pdf is not None:
                self.boxes[self.idx] = self.auto[self.idx]
                self.draw_box()
                self.rebuild()

        # ---------- actions
        def do_save(self):
            if self.label_pdf is None:
                return
            out = filedialog.asksaveasfilename(defaultextension=".pdf",
                                               initialfile=default_out(self.path).name,
                                               initialdir=str(Path(self.path).parent),
                                               filetypes=[("PDF files", "*.pdf")])
            if not out:
                return
            try:
                Path(out).write_bytes(self.label_pdf)
            except PermissionError:
                messagebox.showerror(APP_NAME, "That file is open somewhere else — close it and try again.")
                return
            self.set_status(f"Saved {Path(out).name}")

        def do_print(self):
            if self.label_pdf is None or not self.printers or str(self.print_b.cget("state")) == "disabled":
                return
            printer = self.printer_menu.get()
            copies = int(self.copies.get())
            save_settings(printer=printer)
            self.print_b.configure(state="disabled", text="Printing…")
            self.set_status(f"Sending to {printer}…")
            self.print_result = None
            data = self.label_pdf

            def work():
                try:
                    print_label(data, printer, copies)
                    self.print_result = ("ok", printer)
                except Exception as ex:
                    self.print_result = ("err", str(ex))

            threading.Thread(target=work, daemon=True).start()
            self.after(100, self._check_print)

        def _check_print(self):
            if self.print_result is None:
                self.after(100, self._check_print)
                return
            kind, info = self.print_result
            self.print_b.configure(text="Print label")
            self._update_controls()
            if kind == "ok":
                self.set_status(f"✓ Sent to {info}")
            else:
                self.set_status("")
                messagebox.showerror(APP_NAME, f"Printing failed:\n\n{info}")

    App().mainloop()


# ============================================================ entry

def main():
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    if files and ("--auto" in sys.argv or "--print" in sys.argv):
        for f in files:
            pdf = normalize_pdf(f)
            label = build_4x6(pdf, auto_boxes(pdf))
            if "--print" in sys.argv:
                names, default = list_printers()
                print_label(label, pick_printer(names, default))
            else:
                out = default_out(f)
                out.write_bytes(label)
                open_file(out)
    else:
        run_gui(files[0] if files else None)


if __name__ == "__main__":
    main()

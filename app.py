import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from flask import Flask, render_template, request, send_from_directory, redirect, url_for, flash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "epub-local-library"

UPLOAD_FOLDER = Path(__file__).parent / "storage" / "epubs"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

SORT_OPTIONS = {
    "recent": "Mas recientes",
    "oldest": "Mas antiguos",
    "author": "Autor",
    "title": "Titulo",
}


def unique_filename(directory: Path, filename: str) -> str:
    """Return a filename that doesn't collide with existing files."""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = filename
    counter = 1
    while (directory / candidate).exists():
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def extract_epub_metadata(filepath: Path) -> dict:
    """Extract title and author from an epub file's OPF metadata."""
    meta = {"title": filepath.stem, "author": "Desconocido"}
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            # Find the OPF file via container.xml
            opf_path = None
            if "META-INF/container.xml" in zf.namelist():
                container = ET.fromstring(zf.read("META-INF/container.xml"))
                ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
                rootfile = container.find(".//c:rootfile", ns)
                if rootfile is not None:
                    opf_path = rootfile.get("full-path")

            if not opf_path:
                # Fallback: look for any .opf file
                opf_path = next((n for n in zf.namelist() if n.endswith(".opf")), None)

            if opf_path:
                opf = ET.fromstring(zf.read(opf_path))
                dc = "http://purl.org/dc/elements/1.1/"
                title_el = opf.find(f".//{{{dc}}}title")
                creator_el = opf.find(f".//{{{dc}}}creator")
                if title_el is not None and title_el.text:
                    meta["title"] = title_el.text.strip()
                if creator_el is not None and creator_el.text:
                    meta["author"] = creator_el.text.strip()
    except Exception:
        pass
    return meta


def get_books():
    """Return list of dicts with filename, title, author, and mtime."""
    books = []
    for f in UPLOAD_FOLDER.iterdir():
        if f.suffix.lower() == ".epub":
            meta = extract_epub_metadata(f)
            books.append({
                "filename": f.name,
                "title": meta["title"],
                "author": meta["author"],
                "mtime": f.stat().st_mtime,
            })
    return books


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("No se selecciono ningun archivo.")
            return redirect(url_for("index"))

        filename = secure_filename(file.filename)
        if not filename.lower().endswith(".epub"):
            flash("Solo se permiten archivos .epub.")
            return redirect(url_for("index"))

        filename = unique_filename(UPLOAD_FOLDER, filename)
        file.save(UPLOAD_FOLDER / filename)
        flash(f"Archivo subido: {filename}")
        return redirect(url_for("index"))

    sort_by = request.args.get("sort", "recent")
    if sort_by not in SORT_OPTIONS:
        sort_by = "recent"

    books = get_books()

    if sort_by == "recent":
        books.sort(key=lambda b: b["mtime"], reverse=True)
    elif sort_by == "oldest":
        books.sort(key=lambda b: b["mtime"])
    elif sort_by == "author":
        books.sort(key=lambda b: b["author"].lower())
    elif sort_by == "title":
        books.sort(key=lambda b: b["title"].lower())

    return render_template("index.html", books=books, sort_by=sort_by, sort_options=SORT_OPTIONS)


@app.route("/download/<filename>")
def download(filename):
    filename = secure_filename(filename)
    if not filename.lower().endswith(".epub"):
        return "Archivo no permitido.", 403
    filepath = UPLOAD_FOLDER / filename
    if not filepath.is_file():
        return "Archivo no encontrado.", 404
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=True)

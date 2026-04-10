import argparse
import io
import mimetypes
import os
import posixpath
import re
import sys
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from flask import Flask, jsonify, render_template, request, send_from_directory, send_file, redirect, url_for, flash
from markupsafe import Markup
from werkzeug.utils import secure_filename

# Add acsm_lib to path so its internal imports resolve
ACSM_LIB = Path(__file__).parent / "acsm_lib"
sys.path.insert(0, str(ACSM_LIB))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "epub-local-library")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "ebook-local-library"}), 200

UPLOAD_FOLDER = Path(__file__).parent / "storage" / "epubs"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

ADEPT_DIR = Path(__file__).parent / "storage" / "adept"
ADEPT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".epub", ".acsm"}

SORT_OPTIONS = {
    "recent": "Mas recientes",
    "oldest": "Mas antiguos",
    "author": "Autor",
    "title": "Titulo",
}


def adobe_authorized() -> bool:
    """Check if Adobe device authorization files exist."""
    return all(
        (ADEPT_DIR / f).is_file()
        for f in ("devicesalt", "device.xml", "activation.xml")
    )


def unique_filename(directory: Path, filename: str) -> str:
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = filename
    counter = 1
    while (directory / candidate).exists():
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def get_adobe_key_der() -> bytes | None:
    """Extract the DER decryption key from the Adobe activation data."""
    from libadobeAccount import exportAccountEncryptionKeyBytes
    from libadobe import update_account_path
    update_account_path(str(ADEPT_DIR))
    return exportAccountEncryptionKeyBytes()


def remove_drm(epub_path: str, output_path: str) -> tuple[bool, str]:
    """Remove Adobe DRM from an ePub. Returns (success, message)."""
    from ineptepub import decryptBook
    key_der = get_adobe_key_der()
    if key_der is None:
        return False, "No se pudo obtener la clave de descifrado Adobe."
    result = decryptBook(key_der, epub_path, output_path)
    if result == 0:
        return True, "DRM eliminado correctamente."
    elif result == 1:
        # Book is already DRM-free
        shutil.copy2(epub_path, output_path)
        return True, "El libro no tiene DRM."
    else:
        return False, "No se pudo eliminar el DRM."


def fulfill_acsm(acsm_path: Path) -> tuple[Path | None, str | None]:
    """Fulfill an ACSM file to a DRM-free ePub.

    Steps: fulfill -> download -> inject rights.xml -> remove DRM.
    Returns (epub_path, error_message). On success error is None.
    """
    from libadobe import update_account_path, sendHTTPRequest_DL2FILE
    from libadobeFulfill import buildRights, fulfill
    from lxml import etree

    update_account_path(str(ADEPT_DIR))

    try:
        success, reply_data = fulfill(str(acsm_path))
    except Exception as e:
        return None, f"Error en fulfill ACSM: {e}"

    if not success:
        return None, f"Fulfill fallo: {reply_data}"

    # Parse fulfillment response
    adNS = lambda tag: '{%s}%s' % ('http://ns.adobe.com/adept', tag)
    adDC = lambda tag: '{%s}%s' % ('http://purl.org/dc/elements/1.1/', tag)

    try:
        response = etree.fromstring(reply_data)
        download_url = response.find(
            "./%s/%s/%s" % (adNS("fulfillmentResult"), adNS("resourceItemInfo"), adNS("src"))
        ).text
        license_token_node = response.find(
            "./%s/%s/%s" % (adNS("fulfillmentResult"), adNS("resourceItemInfo"), adNS("licenseToken"))
        )
        rights_xml_str = buildRights(license_token_node)
    except Exception as e:
        return None, f"Error parseando respuesta Adobe: {e}"

    if rights_xml_str is None:
        return None, "No se pudo construir rights.xml."

    # Extract book title for filename
    book_name = acsm_path.stem
    try:
        metadata_node = response.find(
            "./%s/%s/%s" % (adNS("fulfillmentResult"), adNS("resourceItemInfo"), adNS("metadata"))
        )
        title = metadata_node.find("./%s" % adDC("title")).text
        if title:
            book_name = title
    except Exception:
        pass

    # Download, inject rights, then remove DRM
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_file = os.path.join(tmpdir, "book.tmp")
        ret = sendHTTPRequest_DL2FILE(download_url, tmp_file)
        if ret != 200:
            return None, f"Descarga fallo con codigo HTTP {ret}."

        with open(tmp_file, "rb") as f:
            header = f.read(10)

        if not header.startswith(b"PK"):
            return None, "El archivo descargado no es un ePub (ZIP)."

        # Inject rights.xml (needed for DRM removal)
        epub_drm = os.path.join(tmpdir, "book_drm.epub")
        shutil.copy2(tmp_file, epub_drm)
        with zipfile.ZipFile(epub_drm, "a") as zf:
            zf.writestr("META-INF/rights.xml", rights_xml_str)

        # Remove DRM
        epub_clean = os.path.join(tmpdir, "book_clean.epub")
        drm_ok, drm_msg = remove_drm(epub_drm, epub_clean)

        if not drm_ok:
            return None, f"Fulfill OK pero fallo DRM removal: {drm_msg}"

        # Move clean epub to storage
        safe_name = secure_filename(book_name + ".epub")
        if not safe_name or safe_name == ".epub":
            safe_name = "book.epub"
        safe_name = unique_filename(UPLOAD_FOLDER, safe_name)
        dest = UPLOAD_FOLDER / safe_name
        shutil.copy2(epub_clean, str(dest))
        return dest, None


def _find_opf(zf: zipfile.ZipFile) -> tuple[str | None, str]:
    """Return (opf_path, opf_dir) from an open ZipFile."""
    opf_path = None
    if "META-INF/container.xml" in zf.namelist():
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
        rootfile = container.find(".//c:rootfile", ns)
        if rootfile is not None:
            opf_path = rootfile.get("full-path")
    if not opf_path:
        opf_path = next((n for n in zf.namelist() if n.endswith(".opf")), None)
    opf_dir = posixpath.dirname(opf_path) if opf_path else ""
    return opf_path, opf_dir


def find_cover_path(filepath: Path) -> str | None:
    """Find the cover image path inside an ePub."""
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            opf_path, opf_dir = _find_opf(zf)
            if not opf_path:
                return None
            opf = ET.fromstring(zf.read(opf_path))
            opf_ns = ""
            if opf.tag.startswith("{"):
                opf_ns = opf.tag.split("}")[0] + "}"

            # Build manifest map
            manifest = {}
            for item in opf.findall(f".//{opf_ns}item"):
                manifest[item.get("id")] = item

            # Method 1: <item properties="cover-image"> (EPUB3)
            for item in manifest.values():
                props = item.get("properties", "")
                if "cover-image" in props:
                    href = item.get("href")
                    return posixpath.join(opf_dir, href) if opf_dir else href

            # Method 2: <meta name="cover" content="item-id"> (EPUB2)
            for meta_el in opf.findall(f".//{opf_ns}meta"):
                if meta_el.get("name") == "cover":
                    cover_id = meta_el.get("content")
                    if cover_id and cover_id in manifest:
                        href = manifest[cover_id].get("href")
                        return posixpath.join(opf_dir, href) if opf_dir else href

            # Method 3: look for common cover filenames
            namelist = zf.namelist()
            for pattern in ("cover.jpg", "cover.jpeg", "cover.png", "Images/cover", "images/cover"):
                for name in namelist:
                    if name.lower().startswith(pattern.lower()) and name.lower().endswith(
                            (".jpg", ".jpeg", ".png", ".gif")):
                        return name
    except Exception:
        pass
    return None


def extract_epub_metadata(filepath: Path, full: bool = False) -> dict:
    meta = {"title": filepath.stem, "author": "Desconocido", "cover": None}
    if full:
        meta.update({
            "language": "", "publisher": "", "date": "",
            "description": "", "subject": "", "rights": "",
            "identifier": "",
        })
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            opf_path, opf_dir = _find_opf(zf)
            if opf_path:
                opf = ET.fromstring(zf.read(opf_path))
                dc = "http://purl.org/dc/elements/1.1/"
                title_el = opf.find(f".//{{{dc}}}title")
                creator_el = opf.find(f".//{{{dc}}}creator")
                if title_el is not None and title_el.text:
                    meta["title"] = title_el.text.strip()
                if creator_el is not None and creator_el.text:
                    meta["author"] = creator_el.text.strip()
                if full:
                    dc_fields = {
                        "language": "language", "publisher": "publisher",
                        "date": "date", "description": "description",
                        "subject": "subject", "rights": "rights",
                        "identifier": "identifier",
                    }
                    for key, tag in dc_fields.items():
                        el = opf.find(f".//{{{dc}}}{tag}")
                        if el is not None and el.text:
                            meta[key] = el.text.strip()
    except Exception:
        pass
    meta["cover"] = find_cover_path(filepath)
    return meta


def get_books():
    books = []
    for f in UPLOAD_FOLDER.iterdir():
        if f.suffix.lower() == ".epub":
            meta = extract_epub_metadata(f)
            books.append({
                "filename": f.name,
                "title": meta["title"],
                "author": meta["author"],
                "cover": meta["cover"],
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
        ext = Path(filename).suffix.lower()

        if ext not in ALLOWED_EXTENSIONS:
            flash("Solo se permiten archivos .epub o .acsm.")
            return redirect(url_for("index"))

        if ext == ".acsm":
            if not adobe_authorized():
                flash("Primero debes autorizar una cuenta Adobe. Ve a Configuracion ACSM.")
                return redirect(url_for("index"))

            with tempfile.NamedTemporaryFile(suffix=".acsm", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                file.save(tmp_path)

            epub_path, error = fulfill_acsm(tmp_path)
            tmp_path.unlink(missing_ok=True)

            if error:
                flash(error)
            else:
                flash(f"ACSM convertido a ePub: {epub_path.name}")
            return redirect(url_for("index"))

        filename = unique_filename(UPLOAD_FOLDER, filename)
        file.save(UPLOAD_FOLDER / filename)
        flash(f"Archivo subido: {filename}")
        return redirect(url_for("index"))

    sort_by = request.args.get("sort", "recent")
    if sort_by not in SORT_OPTIONS:
        sort_by = "recent"

    query = request.args.get("q", "").strip()

    books = get_books()

    if query:
        q_lower = query.lower()
        books = [b for b in books if q_lower in b["title"].lower() or q_lower in b["author"].lower()]

    if sort_by == "recent":
        books.sort(key=lambda b: b["mtime"], reverse=True)
    elif sort_by == "oldest":
        books.sort(key=lambda b: b["mtime"])
    elif sort_by == "author":
        books.sort(key=lambda b: b["author"].lower())
    elif sort_by == "title":
        books.sort(key=lambda b: b["title"].lower())

    has_acsm = adobe_authorized()
    return render_template("index.html", books=books, sort_by=sort_by,
                           sort_options=SORT_OPTIONS, has_acsm=has_acsm, query=query)


@app.route("/setup-adobe", methods=["GET", "POST"])
def setup_adobe():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if email and not password:
            flash("Si introduces email, debes introducir contrasena.")
            return redirect(url_for("setup_adobe"))

        try:
            from libadobe import createDeviceKeyFile, update_account_path
            from libadobeAccount import createDeviceFile, createUser, signIn, activateDevice

            update_account_path(str(ADEPT_DIR))
            orig_dir = os.getcwd()
            os.chdir(str(ADEPT_DIR))

            try:
                createDeviceKeyFile()

                success = createDeviceFile(True, 1)  # ADE 2.0
                if not success:
                    flash("Error creando archivo de dispositivo.")
                    return redirect(url_for("setup_adobe"))

                success, resp = createUser(1, None)
                if not success:
                    flash(f"Error creando usuario: {resp}")
                    return redirect(url_for("setup_adobe"))

                if email:
                    success, resp = signIn("AdobeID", email, password)
                else:
                    success, resp = signIn("anonymous", "", "")

                if not success:
                    flash(f"Login fallido: {resp}")
                    return redirect(url_for("setup_adobe"))

                success, resp = activateDevice(1, None)
                if not success:
                    flash(f"Error activando dispositivo: {resp}")
                    return redirect(url_for("setup_adobe"))

                label = email if email else "anonima"
                flash(f"Cuenta Adobe autorizada correctamente ({label}).")
            finally:
                os.chdir(orig_dir)

        except Exception as e:
            flash(f"Error: {e}")
            return redirect(url_for("setup_adobe"))

        return redirect(url_for("index"))

    authorized = adobe_authorized()
    return render_template("setup_adobe.html", authorized=authorized)


@app.route("/book/<filename>")
def book_detail(filename):
    from datetime import datetime
    filename = secure_filename(filename)
    if not filename.lower().endswith(".epub"):
        return "Archivo no permitido.", 403
    filepath = UPLOAD_FOLDER / filename
    if not filepath.is_file():
        return "Archivo no encontrado.", 404

    meta = extract_epub_metadata(filepath, full=True)
    stat = filepath.stat()
    meta["filename"] = filename
    meta["filesize"] = stat.st_size
    meta["modified"] = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    return render_template("book_detail.html", book=meta)


@app.route("/delete/<filename>", methods=["POST"])
def delete_book(filename):
    filename = secure_filename(filename)
    if not filename.lower().endswith(".epub"):
        return "Archivo no permitido.", 403
    filepath = UPLOAD_FOLDER / filename
    if not filepath.is_file():
        return "Archivo no encontrado.", 404
    filepath.unlink()
    flash(f"Libro eliminado: {filename}")
    return redirect(url_for("index"))


def get_epub_spine(filepath: Path) -> list[dict]:
    """Return ordered list of spine items: [{id, href, label}]."""
    items = []
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            opf_path = None
            opf_dir = ""
            if "META-INF/container.xml" in zf.namelist():
                container = ET.fromstring(zf.read("META-INF/container.xml"))
                ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
                rootfile = container.find(".//c:rootfile", ns)
                if rootfile is not None:
                    opf_path = rootfile.get("full-path")
            if not opf_path:
                opf_path = next((n for n in zf.namelist() if n.endswith(".opf")), None)
            if not opf_path:
                return items
            opf_dir = posixpath.dirname(opf_path)

            opf = ET.fromstring(zf.read(opf_path))
            # Handle OPF namespace
            opf_ns = ""
            if opf.tag.startswith("{"):
                opf_ns = opf.tag.split("}")[0] + "}"

            # Build manifest map: id -> href
            manifest = {}
            for item in opf.findall(f".//{opf_ns}item"):
                manifest[item.get("id")] = item.get("href")

            # Get spine order
            for itemref in opf.findall(f".//{opf_ns}itemref"):
                idref = itemref.get("idref")
                href = manifest.get(idref, "")
                if href:
                    full_href = posixpath.join(opf_dir, href) if opf_dir else href
                    # Use filename as label
                    label = PurePosixPath(href).stem.replace("_", " ").replace("-", " ").title()
                    items.append({"id": idref, "href": full_href, "label": label})
    except Exception:
        pass
    return items


def extract_chapter_html(filepath: Path, chapter_href: str) -> str | None:
    """Extract a chapter's body content as HTML string."""
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            if chapter_href not in zf.namelist():
                return None
            raw = zf.read(chapter_href)

            # Parse XHTML, extract body content
            # Remove XML declaration and doctype that trip up ET
            text = raw.decode("utf-8", errors="replace")
            # Strip namespace prefixes for easier parsing
            text = re.sub(r'\sxmlns(?::\w+)?="[^"]*"', "", text)
            text = re.sub(r'<\?xml[^>]*\?>', "", text)
            text = re.sub(r'<!DOCTYPE[^>]*>', "", text)

            # Find body content
            body_match = re.search(r"<body[^>]*>(.*)</body>", text, re.DOTALL | re.IGNORECASE)
            if body_match:
                body_html = body_match.group(1).strip()
            else:
                body_html = text.strip()

            return body_html
    except Exception:
        return None


@app.route("/read/<filename>")
def read_book(filename):
    filename = secure_filename(filename)
    if not filename.lower().endswith(".epub"):
        return "Archivo no permitido.", 403
    filepath = UPLOAD_FOLDER / filename
    if not filepath.is_file():
        return "Archivo no encontrado.", 404

    spine = get_epub_spine(filepath)
    if not spine:
        flash("No se pudo leer la estructura del ePub.")
        return redirect(url_for("book_detail", filename=filename))

    # Get chapter index from query param
    chapter_idx = request.args.get("ch", 0, type=int)
    chapter_idx = max(0, min(chapter_idx, len(spine) - 1))

    chapter = spine[chapter_idx]
    body_html = extract_chapter_html(filepath, chapter["href"])
    if body_html is None:
        body_html = "<p>No se pudo cargar este capitulo.</p>"

    # Rewrite image/resource paths in HTML
    chapter_dir = posixpath.dirname(chapter["href"])

    def rewrite_resource(m):
        attr = m.group(1)
        quote = m.group(2)
        src = m.group(3)
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        abs_path = posixpath.normpath(posixpath.join(chapter_dir, src))
        new_url = url_for("epub_resource", filename=filename, resource=abs_path)
        return f'{attr}={quote}{new_url}{quote}'

    body_html = re.sub(r'(src|href)=(["\'])([^"\']+)\2', rewrite_resource, body_html)

    return render_template("reader.html",
                           book_filename=filename,
                           chapter_label=chapter["label"],
                           chapter_html=Markup(body_html),
                           chapter_idx=chapter_idx,
                           total_chapters=len(spine),
                           spine=spine)


@app.route("/epub-resource/<filename>/<path:resource>")
def epub_resource(filename, resource):
    """Serve a resource (image, css, font) from inside an ePub."""
    filename = secure_filename(filename)
    if not filename.lower().endswith(".epub"):
        return "No permitido.", 403
    filepath = UPLOAD_FOLDER / filename
    if not filepath.is_file():
        return "No encontrado.", 404

    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            if resource not in zf.namelist():
                return "Recurso no encontrado.", 404
            data = zf.read(resource)
            mime, _ = mimetypes.guess_type(resource)
            if not mime:
                mime = "application/octet-stream"
            return send_file(io.BytesIO(data), mimetype=mime)
    except Exception:
        return "Error leyendo recurso.", 500


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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", 8000)),
    )
    args = parser.parse_args()
    app.run(host="0.0.0.0", port=args.port)

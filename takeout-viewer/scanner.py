import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.heic', '.mp4', '.mov'}

MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _name_key(name: str) -> str:
    return name.casefold()


def _base_media_key(name: str) -> str:
    """Clave común para originales y variantes editadas exportadas por Takeout."""
    stem = Path(name).stem.casefold()
    stem = re.sub(r"(?:[ _-](?:edited|editado)|~\d+|\s*\(\d+\))$", "", stem)
    return stem


def _media_metadata_keys(path: Path) -> set[str]:
    """Genera las variantes que usa Google Takeout para nombres con copias."""
    keys = {_name_key(path.name), _name_key(path.stem), _base_media_key(path.name)}
    match = re.match(r"^(.*?)\(([0-9]+)\)(\.[^.]+)$", path.name)
    if match:
        base, number, extension = match.groups()
        keys.add(_name_key(f"{base}{extension}({number})"))
    return keys


def _load_metadata(target_path: Path, progress_callback):
    """Carga los JSON una sola vez y los relaciona con el medio de su carpeta."""
    metadata = {}
    global_metadata = {}
    base_metadata = {}
    global_base_metadata = {}
    for json_path in target_path.rglob("*.json"):
        try:
            _report(progress_callback, f"Leyendo metadatos: {json_path.name}")
            with open(json_path, "r", encoding="utf-8-sig") as file_handle:
                data = json.load(file_handle)
            if not isinstance(data, dict):
                continue

            keys = {_name_key(json_path.name[:-5])}
            title = data.get("title")
            if isinstance(title, str) and title:
                keys.update(_media_metadata_keys(Path(title)))
            for key in keys:
                metadata[(json_path.parent, key)] = data
                global_metadata.setdefault(key, []).append(data)
            for key in {_base_media_key(key) for key in keys}:
                base_metadata.setdefault((json_path.parent, key), []).append(data)
                global_base_metadata.setdefault(key, []).append(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    # Un nombre único puede asociarse con seguridad incluso si Takeout lo guardó
    # en un álbum/carpeta distinta al archivo multimedia.
    unique_global_metadata = {
        key: entries[0] for key, entries in global_metadata.items() if len(entries) == 1
    }
    unique_base_metadata = {
        key: entries[0] for key, entries in base_metadata.items() if len(entries) == 1
    }
    unique_global_base_metadata = {
        key: entries[0] for key, entries in global_base_metadata.items() if len(entries) == 1
    }
    return metadata, unique_global_metadata, unique_base_metadata, unique_global_base_metadata


def _taken_datetime(metadata, fallback_timestamp: float):
    if metadata:
        for field in ("photoTakenTime", "creationTime"):
            value = metadata.get(field, {}).get("timestamp")
            try:
                if value not in (None, "", "0", 0):
                    return datetime.fromtimestamp(int(value)), True
            except (TypeError, ValueError, OSError):
                continue
    return datetime.fromtimestamp(fallback_timestamp), False


def _datetime_from_filename(path: Path):
    """Respaldo para screenshots cuyo JSON no fue incluido en el Takeout."""
    match = re.search(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?:[-_ ]?(\d{2})[-_:]?(\d{2})(?:[-_:]?(\d{2}))?)?", path.stem)
    if not match:
        return None
    try:
        year, month, day, hour, minute, second = match.groups()
        return datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return None

def _report(progress_callback, message: str):
    if progress_callback:
        progress_callback(message)


def extract_zip_files_if_needed(root_path: Path, progress_callback=None) -> Path:
    zip_files = list(root_path.glob("*.zip"))
    if not zip_files:
        return root_path

    output_dir = root_path / "_extracted"
    output_dir.mkdir(exist_ok=True)

    message = f"Se encontraron {len(zip_files)} archivo(s) ZIP. Procesando..."
    print(message)
    _report(progress_callback, message)

    for zip_path in zip_files:
        marker_file = output_dir / f".extracted_{zip_path.stem}"
        if marker_file.exists():
            print(f" - Omitido (ya descomprimido previamente): {zip_path.name}")
            continue

        print(f" - Descomprimiendo: {zip_path.name} ...")
        _report(progress_callback, f"Descomprimiendo archivo: {zip_path.name}")
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                for member in zip_ref.infolist():
                    if not member.is_dir():
                        _report(progress_callback, f"Descomprimiendo {zip_path.name}: {Path(member.filename).name}")
                    zip_ref.extract(member, output_dir)
            marker_file.touch()
        except Exception as e:
            print(f"   Error al descomprimir {zip_path.name}: {e}")

    return output_dir

def scan_takeout_directory(root_path: Path, progress_callback=None) -> list:
    target_path = extract_zip_files_if_needed(root_path, progress_callback)

    print(f"Escaneando fotos/videos en: {target_path} ...")
    _report(progress_callback, f"Buscando fotos y videos en: {target_path}")
    items = []
    metadata_by_file, global_metadata, base_metadata, global_base_metadata = _load_metadata(target_path, progress_callback)

    for path in target_path.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
            _report(progress_callback, f"Leyendo archivo: {path.name}")
            metadata = next(
                (
                    data for key in _media_metadata_keys(path)
                    if (data := metadata_by_file.get((path.parent, key))) is not None
                ),
                None,
            )
            if metadata is None:
                metadata = next(
                    (
                        data for key in _media_metadata_keys(path)
                        if (data := global_metadata.get(key)) is not None
                    ),
                    None,
                )
            if metadata is None:
                metadata = base_metadata.get((path.parent, _base_media_key(path.name)))
            if metadata is None:
                metadata = global_base_metadata.get(_base_media_key(path.name))
            taken_at, date_known = _taken_datetime(metadata, os.path.getmtime(path))
            if not date_known:
                filename_date = _datetime_from_filename(path)
                if filename_date:
                    taken_at, date_known = filename_date, True

            # Como último respaldo se usa la fecha del archivo; nunca se crea
            # una categoría separada sin fecha.
            date_known = True

            items.append({
                "id": len(items),
                "title": path.name,
                "abs_path": str(path.resolve()),
                "timestamp": taken_at.timestamp(),
                "year": taken_at.year,
                "date_key": taken_at.strftime("%Y-%m-%d"),
                "date_label": f"{taken_at.day} de {MONTHS_ES[taken_at.month - 1]} de {taken_at.year}",
                "date_known": date_known,
                "is_video": path.suffix.lower() in {'.mp4', '.mov'}
            })

    items.sort(key=lambda x: x['timestamp'], reverse=True)
    print(f"¡Listo! Se encontraron {len(items)} archivos.")
    return items

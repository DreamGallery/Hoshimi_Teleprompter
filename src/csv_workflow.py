"""Read and update the lossless IDOLY PRIDE adventure CSV format."""

from collections import Counter
import csv
import hashlib
import os
from pathlib import Path
import re
import tempfile


COLUMNS = ["id", "name", "text", "trans"]
FIELD_ID = re.compile(r"^\d+:(text|title|choice|narration):\d+$")
PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z_0-9]*(?::[^{}]+)?\}|\{\d+(?::[^{}]+)?\}|<[^>]+>")


def csv_files(directory: Path, names: list[str], prefixes: list[str]) -> list[Path]:
    if names and prefixes:
        raise ValueError("Choose filenames or prefixes, not both")
    if names:
        if len(names) != len(set(names)):
            raise ValueError("Each --file may be selected only once")
        if any(Path(name).name != name or not name.startswith("adv_") or
               not name.endswith(".csv") for name in names):
            raise ValueError("Each --file must be an adv_*.csv filename")
        paths = [directory / name for name in names]
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(path)
        return paths
    return sorted(path for path in directory.glob("adv_*.csv")
                  if not prefixes or path.name.startswith(tuple(prefixes)))


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != COLUMNS:
            raise ValueError(f"{path}: CSV columns must be id,name,text,trans")
        rows = list(reader)
    if len(rows) < 2 or rows[-2]["id"] != "info" or rows[-1]["id"] != "译者":
        raise ValueError(f"{path}: missing source checksum or translator footer")
    if rows[-2]["name"] != path.with_suffix(".txt").name or not re.fullmatch(
            r"[0-9a-f]{64}", rows[-2]["text"]):
        raise ValueError(f"{path}: invalid source filename or SHA-256")
    for row in rows:
        if set(row) != set(COLUMNS) or any(value is None for value in row.values()):
            raise ValueError(f"{path}: malformed CSV row")
    for row in rows[:-2]:
        if not FIELD_ID.fullmatch(row["id"]) or not row["text"]:
            raise ValueError(f"{path}: invalid translatable field")
        if row["trans"]:
            validate_translation(row["text"], row["trans"], row["id"])
    return rows


def validate_translation(source: str, translation: str, identifier: str) -> None:
    if not isinstance(translation, str) or not translation or any(
            char in translation for char in ("\r", "\n", "[", "]")):
        raise ValueError(f"{identifier}: translation contains unsafe script characters")
    if Counter(PLACEHOLDER.findall(source)) != Counter(PLACEHOLDER.findall(translation)):
        raise ValueError(f"{identifier}: placeholders differ from source")
    if source.count(r"\n") != translation.count(r"\n"):
        raise ValueError(f"{identifier}: visible line breaks differ from source")


def normalize_model_translation(source: str, value: str) -> str:
    """Preserve model-intended line breaks and safe bracket glyphs in script text."""
    if not isinstance(value, str):
        return value
    if source.count(r"\n") == value.count(r"\n") + value.count("\n"):
        value = value.replace("\r\n", "\n").replace("\n", r"\n")
    return value.replace("[", "［").replace("]", "］")


def save_csv(path: Path, rows: list[dict[str, str]],
             expected_checksum: str | None = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                     prefix=".translation-", delete=False) as stream:
        temporary = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    try:
        checksum = source_checksum(temporary)
        if expected_checksum is not None and source_checksum(path) != expected_checksum:
            raise ValueError(f"{path}: CSV changed while translation was running; "
                             "reload it before retrying")
        os.replace(temporary, path)
        return checksum
    finally:
        temporary.unlink(missing_ok=True)


def source_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_source(rows: list[dict[str, str]], source_directory: Path | None) -> None:
    if source_directory is None:
        return
    source = source_directory / rows[-2]["name"]
    if not source.is_file() or source_checksum(source) != rows[-2]["text"]:
        raise ValueError(f"{source}: original TXT differs from this CSV")


def coverage(rows: list[dict[str, str]]) -> tuple[int, int]:
    return sum(bool(row["trans"]) for row in rows[:-2]), len(rows) - 2


def translate_file(path: Path, service, glossary: dict[str, str], batch_size: int,
                   source_directory: Path | None = None,
                   max_batches: int | None = None) -> int:
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    csv_checksum = source_checksum(path)
    rows = load_csv(path)
    if source_checksum(path) != csv_checksum:
        raise ValueError(f"{path}: CSV changed while it was being read")
    verify_source(rows, source_directory)
    pending = [index for index, row in enumerate(rows[:-2])
               if not row["trans"]]

    def save_progress() -> None:
        nonlocal csv_checksum
        verify_source(rows, source_directory)
        csv_checksum = save_csv(path, rows, csv_checksum)

    def translate_segments(index: int, batch_number: int) -> int:
        row = rows[index]
        segments = row["text"].split(r"\n")
        translated_segments = []
        for part_number, source_part in enumerate(segments):
            if not source_part:
                translated_segments.append("")
                continue
            identifier = f"{row['id']}:part:{part_number}"
            line = [{"id": identifier, "speaker": row["name"],
                     "source": source_part, "script": path.with_suffix('.txt').name,
                     "field": row['id']}]
            for attempt in range(3):
                try:
                    response = service.translate_batch(line, glossary)
                    if set(response) != {identifier}:
                        raise ValueError("Model omitted or added segment ID")
                    value = normalize_model_translation(source_part, response[identifier])
                    validate_translation(source_part, value, identifier)
                    translated_segments.append(value)
                    break
                except (ValueError, KeyError) as exc:
                    if attempt == 2:
                        raise ValueError(f"{path}: batch {batch_number}, segment "
                                         f"{part_number + 1}: {exc}") from exc
        value = r"\n".join(translated_segments)
        validate_translation(row["text"], value, row["id"])
        row["trans"] = value
        save_progress()
        return 1

    def translate_indices(indices: list[int], batch_number: int) -> int:
        lines = [{"id": rows[index]["id"], "speaker": rows[index]["name"],
                  "source": rows[index]["text"],
                  "script": path.with_suffix('.txt').name,
                  "field": rows[index]["id"]} for index in indices]
        for attempt in range(3):
            try:
                result = service.translate_batch(lines, glossary)
                if set(result) != {line["id"] for line in lines}:
                    raise ValueError("Model omitted or added field IDs")
                for index in indices:
                    row = rows[index]
                    result[row["id"]] = normalize_model_translation(
                        row["text"], result[row["id"]])
                    validate_translation(row["text"], result[row["id"]], row["id"])
                break
            except (ValueError, KeyError) as exc:
                if attempt == 2:
                    if len(indices) > 1:
                        midpoint = len(indices) // 2
                        return (translate_indices(indices[:midpoint], batch_number) +
                                translate_indices(indices[midpoint:], batch_number))
                    if r"\n" in rows[indices[0]]["text"]:
                        return translate_segments(indices[0], batch_number)
                    raise ValueError(f"{path}: batch {batch_number}: {exc}") from exc
        for index in indices:
            rows[index]["trans"] = result[rows[index]["id"]]
        save_progress()
        return len(indices)

    translated = 0
    for offset in range(0, len(pending), batch_size):
        if max_batches is not None and offset // batch_size >= max_batches:
            break
        translated += translate_indices(pending[offset:offset + batch_size],
                                        offset // batch_size + 1)
    return translated

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx
from mistralai import Mistral
from dotenv import load_dotenv
from tqdm import tqdm

CleanupFn = Callable[[], None]


@dataclass(frozen=True)
class Config:
    mistral_api_key: str
    ocr_endpoint: str
    ocr_model: str
    text_model: str
    upload_provider: str
    supabase_url: Optional[str]
    supabase_anon_key: Optional[str]
    supabase_bucket: str
    delete_after_ocr: bool


def _read_json_file(path: Path) -> Dict[str, object]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return data


def load_config(config_path: Path) -> Config:
    raw = _read_json_file(config_path)
    mistral_api_key = _as_optional_str(raw.get("mistral_api_key"))
    if mistral_api_key is None or mistral_api_key == "SET_ME":
        env_key = os.environ.get("MISTRAL_API_KEY")
        if not isinstance(env_key, str) or not env_key.strip():
            raise ValueError("Missing MISTRAL_API_KEY (set it in .env or config.json)")
        mistral_api_key = env_key.strip()

    ocr = raw.get("ocr")
    if not isinstance(ocr, dict):
        raise ValueError("Config key `ocr` must be an object")
    ocr_endpoint = _as_required_str(ocr.get("endpoint"), "ocr.endpoint")
    ocr_model = _as_required_str(ocr.get("model"), "ocr.model")

    text_model = _as_required_str(raw.get("text_model"), "text_model")
    upload_provider = _as_optional_str(raw.get("upload_provider")) or "auto"
    supabase_url = _as_optional_str(raw.get("supabase_url"))
    supabase_anon_key = _as_optional_str(raw.get("supabase_anon_key"))
    supabase_bucket = _as_optional_str(raw.get("supabase_bucket")) or "mistral-tmp"
    delete_after_ocr = bool(raw.get("delete_after_ocr", True))
    return Config(
        mistral_api_key=mistral_api_key,
        ocr_endpoint=ocr_endpoint,
        ocr_model=ocr_model,
        text_model=text_model,
        upload_provider=upload_provider,
        supabase_url=supabase_url,
        supabase_anon_key=supabase_anon_key,
        supabase_bucket=supabase_bucket,
        delete_after_ocr=delete_after_ocr,
    )


def _as_required_str(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing/invalid required config key: {key}")
    return value.strip()


def _as_optional_str(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v else None


def load_config_from_env() -> Config:
    """
    Loads config from environment variables (after dotenv is loaded).
    """
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("Missing MISTRAL_API_KEY (set it in .env)")

    ocr_endpoint = os.environ.get("MISTRAL_OCR_ENDPOINT", "https://api.mistral.ai/v1/ocr")
    ocr_model = os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest")
    text_model = os.environ.get("MISTRAL_TEXT_MODEL", "mistral-small-latest")
    upload_provider = os.environ.get("MISTRAL_UPLOAD_PROVIDER", "auto")
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_anon_key = os.environ.get("SUPABASE_ANON_KEY")
    supabase_bucket = os.environ.get("SUPABASE_BUCKET", "mistral-tmp")
    delete_after_ocr = os.environ.get("DELETE_AFTER_OCR", "true").strip().lower() not in {"0", "false", "no"}

    return Config(
        mistral_api_key=api_key.strip(),
        ocr_endpoint=ocr_endpoint.strip(),
        ocr_model=ocr_model.strip(),
        text_model=text_model.strip(),
        upload_provider=upload_provider.strip(),
        supabase_url=supabase_url.strip() if isinstance(supabase_url, str) and supabase_url.strip() else None,
        supabase_anon_key=supabase_anon_key.strip() if isinstance(supabase_anon_key, str) and supabase_anon_key.strip() else None,
        supabase_bucket=supabase_bucket.strip(),
        delete_after_ocr=delete_after_ocr,
    )


def find_pdfs(input_dir: Path) -> List[Path]:
    return sorted([p for p in input_dir.glob("*.pdf") if p.is_file()])


def safe_stem(path: Path) -> str:
    # Keep it filesystem-friendly
    stem = path.stem
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", stem).strip("_")
    return stem or "pdf"


def _auth_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _try_parse_json_object(text: str) -> Optional[Dict[str, object]]:
    """
    Best-effort extraction of a JSON object from a model response.
    This deliberately avoids `eval` and only returns a dict if valid JSON is found.
    """
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    # Try to find the first {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def mistral_ocr_pdf(
    *,
    api_key: str,
    endpoint: str,
    model: str,
    pdf_bytes: bytes,
    filename: str,
    upload_provider: str,
    supabase_url: Optional[str],
    supabase_anon_key: Optional[str],
    supabase_bucket: str,
    delete_after_ocr: bool,
    timeout_s: float = 120.0,
) -> Dict[str, object]:
    """
    Calls Mistral's OCR endpoint with a few payload shapes for compatibility.

    IMPORTANT:
    - The official OCR endpoint/schema has evolved; this function retries a few shapes
      and returns the parsed JSON response on success.
    - If all attempts fail, it raises with the last response body included.
    """
    errors: List[str] = []
    with httpx.Client(timeout=timeout_s) as client:
        # The current API validation indicates it expects a `document` with type `document_url`.
        document_url, cleanup = upload_pdf_for_ocr(
            client=client,
            pdf_bytes=pdf_bytes,
            filename=filename,
            provider=upload_provider,
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            supabase_bucket=supabase_bucket,
        )
        try:
            payload = {"model": model, "document": {"type": "document_url", "document_url": document_url}}
            resp = client.post(endpoint, headers=_auth_headers(api_key), json=payload)
            if 200 <= resp.status_code < 300:
                try:
                    data = resp.json()
                except ValueError as e:
                    raise RuntimeError(f"OCR response was not JSON: {resp.text[:2000]}") from e
                if not isinstance(data, dict):
                    raise RuntimeError("OCR response JSON was not an object")
                return data
            errors.append(f"HTTP {resp.status_code}: {resp.text[:4000]}")
            raise RuntimeError(f"OCR failed for {filename}. Errors: {errors}")
        finally:
            if delete_after_ocr:
                try:
                    cleanup()
                except Exception:
                    # Best-effort cleanup only; don't hide OCR errors.
                    pass


def upload_pdf_for_ocr(
    *,
    client: httpx.Client,
    pdf_bytes: bytes,
    filename: str,
    provider: str,
    supabase_url: Optional[str],
    supabase_anon_key: Optional[str],
    supabase_bucket: str,
) -> Tuple[str, CleanupFn]:
    """
    Mistral OCR expects a publicly accessible `document_url`.
    For this demo pipeline, we can temporarily upload the PDF and pass the returned URL.
    """
    provider = provider.strip()
    if provider == "auto":
        errors: List[str] = []
        for candidate in ("supabase", "tmpfiles_org", "transfer_sh"):
            try:
                return upload_pdf_for_ocr(
                    client=client,
                    pdf_bytes=pdf_bytes,
                    filename=filename,
                    provider=candidate,
                    supabase_url=supabase_url,
                    supabase_anon_key=supabase_anon_key,
                    supabase_bucket=supabase_bucket,
                )
            except Exception as e:
                errors.append(f"{candidate}: {type(e).__name__}: {e}")
        raise RuntimeError(f"All upload providers failed for {filename}: {errors}")

    if provider == "supabase":
        if supabase_url is None or supabase_anon_key is None:
            raise ValueError("SUPABASE_URL and SUPABASE_ANON_KEY are required for upload_provider=supabase")
        bucket = supabase_bucket.strip() or "mistral-tmp"
        # Create a unique-ish object path to avoid collisions.
        ts = int(time.time())
        safe_name = safe_stem(Path(filename))
        object_path = f"mistral_ocr/{ts}_{safe_name}.pdf"
        put_url = f"{supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_path}"
        headers = {
            "Authorization": f"Bearer {supabase_anon_key}",
            "apikey": supabase_anon_key,
            "Content-Type": "application/pdf",
            "x-upsert": "true",
        }
        resp = client.post(put_url, headers=headers, content=pdf_bytes)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Supabase upload failed: HTTP {resp.status_code} {resp.text[:2000]}")

        public_url = f"{supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/{object_path}"

        # Mistral requires a URL accessible without auth headers.
        # If the bucket is not truly public, fall back to a signed URL.
        document_url = public_url
        try:
            check = client.get(public_url, timeout=10.0)
            if not (200 <= check.status_code < 300):
                raise RuntimeError(f"public_url not accessible (HTTP {check.status_code})")
        except Exception:
            sign_url = f"{supabase_url.rstrip('/')}/storage/v1/object/sign/{bucket}/{object_path}"
            sign_resp = client.post(
                sign_url,
                headers={
                    "Authorization": f"Bearer {supabase_anon_key}",
                    "apikey": supabase_anon_key,
                    "Content-Type": "application/json",
                },
                json={"expiresIn": 600},
            )
            if not (200 <= sign_resp.status_code < 300):
                raise RuntimeError(
                    f"Supabase sign-url failed (bucket may not be public): HTTP {sign_resp.status_code} {sign_resp.text[:2000]}"
                )
            try:
                sign_data = sign_resp.json()
            except ValueError as e:
                raise RuntimeError(f"Supabase sign-url response was not JSON: {sign_resp.text[:2000]}") from e
            signed_path = sign_data.get("signedURL")
            if not isinstance(signed_path, str) or not signed_path.strip():
                raise RuntimeError(f"Supabase sign-url response missing signedURL: {json.dumps(sign_data)[:2000]}")
            signed_path = signed_path.strip()
            document_url = (
                f"{supabase_url.rstrip('/')}{signed_path}" if signed_path.startswith("/") else signed_path
            )

        def _cleanup() -> None:
            del_url = f"{supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_path}"
            del_resp = client.delete(
                del_url,
                headers={"Authorization": f"Bearer {supabase_anon_key}", "apikey": supabase_anon_key},
            )
            # best-effort
            _ = del_resp.status_code

        return document_url, _cleanup

    if provider == "tmpfiles_org":
        resp = client.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": (filename, pdf_bytes, "application/pdf")},
        )
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
            except ValueError as e:
                raise RuntimeError(f"tmpfiles.org response was not JSON: {resp.text[:2000]}") from e

            url = _find_first_http_url(data)
            if url is None:
                raise RuntimeError(f"tmpfiles.org response missing URL: {json.dumps(data)[:2000]}")
            # tmpfiles.org often returns a view URL; convert to direct download if needed.
            if "tmpfiles.org/" in url and "/dl/" not in url:
                parts = url.split("tmpfiles.org/", 1)
                if len(parts) == 2 and parts[1]:
                    url = f"https://tmpfiles.org/dl/{parts[1].lstrip('/')}"
            return url, (lambda: None)
        raise RuntimeError(f"tmpfiles.org upload failed for {filename}: HTTP {resp.status_code} {resp.text[:2000]}")

    if provider == "transfer_sh":
        # transfer.sh supports simple PUT uploads and returns the public URL in the response body.
        upload_url = f"https://transfer.sh/{filename}"
        resp = client.put(
            upload_url,
            content=pdf_bytes,
            headers={"Content-Type": "application/pdf"},
        )
        if 200 <= resp.status_code < 300:
            url = resp.text.strip()
            if url.startswith("http"):
                return url, (lambda: None)
        raise RuntimeError(f"transfer.sh upload failed for {filename}: HTTP {resp.status_code} {resp.text[:2000]}")

    raise ValueError(f"Unsupported upload_provider: {provider}")


def _find_first_http_url(value: object) -> Optional[str]:
    if isinstance(value, str):
        return value if value.startswith("http") else None
    if isinstance(value, dict):
        for v in value.values():
            u = _find_first_http_url(v)
            if u is not None:
                return u
    if isinstance(value, list):
        for v in value:
            u = _find_first_http_url(v)
            if u is not None:
                return u
    return None


def extract_text_from_ocr_payload(ocr_payload: Dict[str, object]) -> str:
    """
    Best-effort extraction of text/markdown from the OCR JSON response.
    """
    # Common shapes:
    # - {"pages":[{"markdown": "..."}]}
    # - {"pages":[{"text": "..."}]}
    # - {"markdown": "..."} or {"text": "..."}
    parts: List[str] = []

    pages = ocr_payload.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            md = page.get("markdown")
            if isinstance(md, str) and md.strip():
                parts.append(md)
                continue
            txt = page.get("text")
            if isinstance(txt, str) and txt.strip():
                parts.append(txt)
                continue

    top_md = ocr_payload.get("markdown")
    if isinstance(top_md, str) and top_md.strip():
        parts.append(top_md)

    top_txt = ocr_payload.get("text")
    if isinstance(top_txt, str) and top_txt.strip():
        parts.append(top_txt)

    if parts:
        return "\n\n".join(parts).strip()

    # Fallback: stringify the JSON (still useful for debugging)
    return json.dumps(ocr_payload, indent=2, ensure_ascii=False)


def chunk_text(text: str, *, max_chars: int) -> List[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    text = text.strip()
    if not text:
        return []

    chunks: List[str] = []
    i = 0
    while i < len(text):
        j = min(len(text), i + max_chars)
        # Try to split on a boundary.
        boundary = text.rfind("\n\n", i, j)
        if boundary == -1 or boundary <= i + int(max_chars * 0.6):
            boundary = j
        chunk = text[i:boundary].strip()
        if chunk:
            chunks.append(chunk)
        i = boundary
    return chunks


def mistral_extract_pricebook_json(
    *,
    api_key: str,
    model: str,
    full_text: str,
    source_name: str,
    max_chars_per_chunk: int = 18000,
) -> Dict[str, object]:
    client = Mistral(api_key=api_key)
    chunks = chunk_text(full_text, max_chars=max_chars_per_chunk)
    if not chunks:
        return {"source": source_name, "error": "empty_ocr_text"}

    merged: Dict[str, object] = {
        "source": source_name,
        "rules": [],
        "tables": [],
        "notes": [],
        "unparsed_chunks": [],
    }

    # Visible progress so long structuring runs don't look "stuck".
    for idx, chunk in enumerate(tqdm(chunks, desc=f"Structuring ({source_name})", unit="chunk"), start=1):
        prompt = (
            "You are extracting a structured 'price book' from OCR text.\n"
            "Return ONLY valid JSON (no markdown, no commentary).\n"
            "\n"
            "Schema requirements:\n"
            "- rules: array of {text: string, page_hint: string|null}\n"
            "- tables: array of {title: string, table_markdown: string, page_hint: string|null}\n"
            "- notes: array of {text: string, page_hint: string|null}\n"
            "\n"
            f"Source: {source_name}\n"
            f"Chunk {idx} of {len(chunks)}\n"
            "\n"
            "OCR TEXT:\n"
            f"{chunk}\n"
        )

        resp = client.chat.complete(
            model=model,
            messages=[
                {"role": "system", "content": "You are a meticulous data extraction engine."},
                {"role": "user", "content": prompt},
            ],
        )

        content = _chat_content_to_text(resp)
        parsed = _try_parse_json_object(content)
        if parsed is None:
            cast_list = merged.get("unparsed_chunks")
            if isinstance(cast_list, list):
                cast_list.append({"chunk_index": idx, "raw": content})
            continue

        _merge_extraction(merged, parsed)

    return merged


def _chat_content_to_text(resp: object) -> str:
    # `mistralai` returns structured response objects; handle both dict-like and attribute-like.
    choices = getattr(resp, "choices", None)
    if isinstance(choices, list) and choices:
        msg = getattr(choices[0], "message", None)
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            return content

    # Fallback for unexpected response shapes
    return str(resp)


def _merge_extraction(target: Dict[str, object], addition: Dict[str, object]) -> None:
    for key in ("rules", "tables", "notes"):
        incoming = addition.get(key)
        if not isinstance(incoming, list):
            continue
        existing = target.get(key)
        if not isinstance(existing, list):
            continue
        for item in incoming:
            if isinstance(item, dict):
                existing.append(item)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, data: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def process_pdf(
    *,
    pdf_path: Path,
    out_dir: Path,
    cfg: Config,
    run_structuring: bool,
) -> Tuple[Path, Path]:
    pdf_bytes = pdf_path.read_bytes()
    name = pdf_path.name
    stem = safe_stem(pdf_path)
    base_out = out_dir / stem

    ocr_payload = mistral_ocr_pdf(
        api_key=cfg.mistral_api_key,
        endpoint=cfg.ocr_endpoint,
        model=cfg.ocr_model,
        pdf_bytes=pdf_bytes,
        filename=name,
        upload_provider=cfg.upload_provider,
        supabase_url=cfg.supabase_url,
        supabase_anon_key=cfg.supabase_anon_key,
        supabase_bucket=cfg.supabase_bucket,
        delete_after_ocr=cfg.delete_after_ocr,
    )

    ocr_text = extract_text_from_ocr_payload(ocr_payload)
    ocr_raw_path = base_out / "ocr_raw.json"
    ocr_text_path = base_out / "ocr_text.md"
    write_json(ocr_raw_path, ocr_payload)
    write_text(ocr_text_path, ocr_text)

    structured_path = base_out / "pricebook_extracted.json"
    if run_structuring:
        structured = mistral_extract_pricebook_json(
            api_key=cfg.mistral_api_key,
            model=cfg.text_model,
            full_text=ocr_text,
            source_name=name,
        )
        write_json(structured_path, structured)

    return (ocr_raw_path, structured_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract price-book data from PDFs using Mistral OCR + structuring.")
    parser.add_argument(
        "--config",
        required=False,
        help="Path to config JSON (copy and edit config.example.json).",
    )
    parser.add_argument("--input-dir", required=True, help="Directory containing PDFs.")
    parser.add_argument(
        "--pdf",
        action="append",
        default=[],
        help="Optional: process only a specific PDF filename (repeatable). If omitted, processes all PDFs in --input-dir.",
    )
    parser.add_argument("--output-dir", required=True, help="Output directory for extracted artifacts.")
    parser.add_argument(
        "--no-structure",
        action="store_true",
        help="Only run OCR and save raw text; skip the structuring pass.",
    )
    args = parser.parse_args()

    # In some execution contexts (e.g. `python -c` / stdin), python-dotenv's auto
    # discovery can fail due to missing stack frames. Be explicit about the path.
    load_dotenv(dotenv_path=Path.cwd() / ".env")
    cfg = load_config(Path(args.config)) if isinstance(args.config, str) and args.config.strip() else load_config_from_env()
    input_dir = Path(args.input_dir)
    out_dir = Path(args.output_dir)

    pdfs = find_pdfs(input_dir)
    if args.pdf:
        wanted = {p.strip() for p in args.pdf if isinstance(p, str) and p.strip()}
        pdfs = [p for p in pdfs if p.name in wanted]
    if not pdfs:
        raise SystemExit(f"No PDFs found in {input_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)

    for pdf in tqdm(pdfs, desc="PDFs"):
        process_pdf(
            pdf_path=pdf,
            out_dir=out_dir,
            cfg=cfg,
            run_structuring=not bool(args.no_structure),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())



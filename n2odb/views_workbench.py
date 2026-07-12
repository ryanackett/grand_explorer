"""Assisted-upload workbench — Stage 0 (source-document capture).

Phase A of the manuscript-extraction plan: attach a PDF/DOCX/pasted paper to a
submission, extract its text synchronously on upload, run the deterministic
parsers, and expose a re-runnable model-extraction endpoint. Nothing here commits
study data — it produces suggestions on the draft.

Endpoints (all scoped by the submission's ``public_token``):
  GET  /submit-data/                          → get/create a draft, render Stage 0
  POST /workbench/<token>/document/           → create ManuscriptSource + extract text
  POST /workbench/<token>/document/extract/   → run/re-run extract_facts()
  GET  /workbench/<token>/document/text/       → serve extracted_text for the panel
  POST /workbench/<token>/data/               → parse a CSV/XLSX into parsed_content
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.http import Http404, JsonResponse, HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import Treatment, RawMeasurementTreatment
from .models_upload import (
    UploadSubmission,
    ManuscriptSource,
    UploadedDataFile,
    LlmExtractionCall,
)
from .services import (
    canonical,
    event_fields,
    llm_assist,
    manuscript_extract,
    manuscript_regex,
    site_fields,
    suggest,
    tabular,
    units,
)
from .services.manuscript_merge import merge_metadata
from .services.manuscript_text import extract_text

# How many sample rows to send back to the browser for the parse preview.
_PREVIEW_ROWS = 8
# Sample values shown per column on the mapping screen.
_MAP_SAMPLES = 4
# A suggestion at/above this confidence is auto-applied on first visit.
_AUTO_APPLY_CONFIDENCE = 0.85

logger = logging.getLogger(__name__)

# Magic bytes for the two binary document types we accept.
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"  # DOCX is a zip container

# User-facing wizard steps, shown in the progress rail on every stage so a
# contributor always knows where they are and how much is left. Kept here as the
# single source of truth; each stage passes its own 1-based ``current_step``.
WIZARD_STEPS = [
    "Upload",
    "Map Temporal Data",
    "Site Information",
    "Soil & Management",
    "Timeline",
    "Review",
    "Submit",
]


# Token-scoped URL name for each 1-based step that has a landing page. Step 7
# (Submit) isn't built yet, so it never becomes a link.
_STEP_URLS = {
    2: "workbench-mapping",
    3: "workbench-site",
    4: "workbench-soil",
    5: "workbench-timeline",
    6: "workbench-review",
    7: "workbench-submit",
}


def _wizard_context(current_step: int, token=None) -> dict:
    """Progress-rail context. Each step carries a link, but only to a page the
    contributor has already reached (step number ≤ the current step); later steps
    stay inert so you can't skip ahead past unfinished work."""
    from django.urls import reverse

    steps = []
    for i, label in enumerate(WIZARD_STEPS, start=1):
        url = None
        if i <= current_step:
            if i == 1:
                url = reverse("submit-data")  # resumes the session's draft
            elif i in _STEP_URLS and token is not None:
                url = reverse(_STEP_URLS[i], args=[token])
        steps.append({
            "num": i,
            "label": label,
            "url": url,
            "state": "done" if i < current_step else ("active" if i == current_step else ""),
        })
    return {
        "wizard_steps": steps,
        "current_step": current_step,
        "current_step_label": WIZARD_STEPS[current_step - 1],
        "wizard_step_count": len(WIZARD_STEPS),
    }


def _is_empty_draft(sub: UploadSubmission) -> bool:
    """A draft with nothing in it yet — no data file, no document, no filled metadata.
    Such drafts are throwaway: reuse them rather than pile up new ones, and hide them
    from the contributor's submissions list."""
    if (sub.parsed_content or {}).get("sheets"):
        return False
    if sub.metadata_values or sub.treatment_details or sub.events:
        return False
    return not (sub.documents.exists() or sub.data_files.exists())


def _get_or_create_draft(request) -> UploadSubmission:
    """The signed-in user's active draft. Resumes the session's draft; failing that,
    reuses an existing *empty* draft rather than spawning throwaway ones; only then
    creates a fresh draft."""
    token = request.session.get("workbench_token")
    if token:
        sub = UploadSubmission.objects.filter(
            public_token=token, status="draft", owner=request.user
        ).first()
        if sub:
            return sub
    for sub in UploadSubmission.objects.filter(owner=request.user, status="draft").order_by("-updated_at"):
        if _is_empty_draft(sub):
            request.session["workbench_token"] = str(sub.public_token)
            return sub
    sub = UploadSubmission.objects.create(owner=request.user)
    request.session["workbench_token"] = str(sub.public_token)
    return sub


def _owned_submission(request, token) -> UploadSubmission:
    """Fetch a submission by token, but only for its owner (or staff). 404 for anyone
    else — a valid-but-unauthorized token must be indistinguishable from a bad one, so
    the UUID never leaks 'this submission exists' to a non-owner."""
    sub = get_object_or_404(UploadSubmission.objects, public_token=token)
    if request.user.is_staff or sub.owner_id == request.user.id:
        return sub
    raise Http404()


def _upload_rejection(upload, existing_count: int) -> JsonResponse | None:
    """Reject an upload that's too large or over the per-submission file cap, else None.

    Files are read fully into memory and stored as a DB blob, so both bounds matter:
    size (OOM / MySQL ``max_allowed_packet``) and count (unbounded blob accumulation).
    ``upload`` is None for pasted text — size is skipped, but it still counts.
    """
    cap_mb = getattr(settings, "WORKBENCH_MAX_UPLOAD_MB", 25)
    if upload is not None and (upload.size or 0) > cap_mb * 1024 * 1024:
        return JsonResponse(
            {"error": f"That file is too large — the limit is {cap_mb} MB."}, status=400
        )
    max_files = getattr(settings, "WORKBENCH_MAX_FILES_PER_SUBMISSION", 15)
    if existing_count >= max_files:
        return JsonResponse(
            {"error": f"This submission already has {max_files} files of this kind. "
                      f"Start a new submission to add more."},
            status=400,
        )
    return None


def _verified_email(user) -> str:
    """The user's verified (login) email — the address a submission is attributed to.

    Contributors can't be attributed to, or notified at, a hand-typed address (that
    would let a fake ``.edu`` inflate the curator's trust signal, or send our decision
    mail to an arbitrary third party). Login already requires a verified email, so we
    bind the submission to it. Prefers the allauth primary/verified record; falls back
    to ``user.email``.
    """
    if user is None:
        return ""
    try:
        from allauth.account.models import EmailAddress
        ea = (EmailAddress.objects
              .filter(user=user, verified=True)
              .order_by("-primary")
              .first())
        if ea:
            return ea.email
    except Exception:  # noqa: BLE001 — never break submit on a lookup hiccup
        pass
    return getattr(user, "email", "") or ""


def submit_data(request):
    """Render the wizard's Stage-0 page for the session's draft submission."""
    submission = _get_or_create_draft(request)
    document = submission.documents.order_by("-created_at").first()
    has_data = bool((submission.parsed_content or {}).get("sheets"))
    # Was the *current* document already analysed successfully? Drives the "re-run"
    # confirm so a contributor knows a re-run is a fresh (billable) AI request.
    already_extracted = bool(
        document and document.sha256
        and document.sha256 == submission.manuscript_source_sha256
    )
    return render(
        request,
        "submit_data.html",
        {
            "submission": submission,
            "public_token": submission.public_token,
            "document": document,
            "has_data": has_data,
            "already_extracted": already_extracted,
            **_wizard_context(current_step=1, token=submission.public_token),
        },
    )


def my_submissions(request):
    """The signed-in contributor's own submissions (newest first) — the resume/track
    page that replaces sharing raw bearer URLs."""
    subs = UploadSubmission.objects.filter(owner=request.user).order_by("-updated_at")
    rows = []
    for s in subs:
        if s.status == "draft" and _is_empty_draft(s):
            continue  # a blank, never-touched draft — nothing to resume
        rows.append({
            "token": s.public_token,
            "title": _resolved_global_value(s, "pub_title") or "(untitled draft)",
            "status": s.status,
            "status_label": s.get_status_display(),
            "updated_at": s.updated_at,
            # Editable states: a live draft, or one a curator sent back for changes.
            "resumable": s.status in ("draft", "changes_requested"),
        })
    return render(request, "workbench_my_submissions.html", {"rows": rows})


def resume_submission(request, token):
    """Reopen one of the contributor's own submissions in the wizard. A submission a
    curator sent back (``changes_requested``) flips back to ``draft`` so it's editable
    again and can be re-submitted; a live draft just reopens."""
    sub = _owned_submission(request, token)
    if sub.status == "changes_requested":
        sub.status = "draft"
        sub.save(update_fields=["status", "updated_at"])
    request.session["workbench_token"] = str(sub.public_token)
    return redirect("submit-data")


def _sniff_kind(filename: str, head: bytes) -> str | None:
    """Determine document kind from magic bytes, using the name only to break the
    zip/docx tie. Never trust the browser's content-type (§9)."""
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_ZIP_MAGIC):
        # Every DOCX is a zip; only treat it as DOCX if it names itself one.
        if filename.lower().endswith(".docx"):
            return "docx"
        return None
    return None


@require_POST
def create_document(request, token):
    """Attach a document to the submission and extract its text synchronously."""
    submission = _owned_submission(request, token)

    upload = request.FILES.get("document")
    pasted = request.POST.get("text", "")

    rejection = _upload_rejection(upload, submission.documents.count())
    if rejection is not None:
        return rejection

    if upload is not None:
        data = upload.read()
        kind = _sniff_kind(upload.name or "", data[:8])
        if kind is None:
            return JsonResponse(
                {"error": "Unsupported file — please upload a PDF or DOCX."},
                status=400,
            )
        original_name = upload.name or ""
        file_bytes = data
        extract_input: bytes | str = data
    elif pasted.strip():
        kind = "text"
        original_name = ""
        file_bytes = None
        extract_input = pasted
        data = pasted.encode("utf-8")
    else:
        return JsonResponse(
            {"error": "No document provided — attach a file or paste text."},
            status=400,
        )

    result = extract_text(kind, extract_input, filename=original_name)

    doc = ManuscriptSource.objects.create(
        submission=submission,
        kind=kind,
        original_name=original_name,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        file_bytes=file_bytes,
        extracted_text=result.text,
        page_count=result.page_count,
        char_count=result.char_count,
        extraction_status="failed" if result.error else "extracted",
        extraction_error=result.error,
    )

    # Deterministic parsers run regardless of the model (§4); seed the draft's
    # facts with whatever they find so DOI/coords are suggested immediately.
    regex_meta = manuscript_regex.parse_all(result.text)
    facts = submission.manuscript_facts or {}
    facts["metadata"] = merge_metadata(regex_meta, facts.get("metadata", {}))
    facts.setdefault("events", [])
    submission.manuscript_facts = facts
    submission.save(update_fields=["manuscript_facts", "updated_at"])

    if result.error:
        return JsonResponse(
            {
                "status": "failed",
                "document_id": doc.id,
                "message": "We couldn't read that document, but you can still "
                "continue and fill the forms in yourself.",
                "detail": result.error,
            }
        )

    word_count = len(result.text.split())
    return JsonResponse(
        {
            "status": "extracted",
            "document_id": doc.id,
            "kind": kind,
            "page_count": result.page_count,
            "char_count": result.char_count,
            "word_count": word_count,
            "regex_suggestions": len(regex_meta),
        }
    )


def _already_have(submission: UploadSubmission) -> list[str]:
    """Field ids the tabular mapping already supplies, so the module skips them."""
    mapping = submission.mapping or {}
    have: set[str] = set()
    for entry in mapping.values():
        if isinstance(entry, dict):
            field = entry.get("canonical_field")
            if field:
                have.add(field)
    return sorted(have)


def _llm_calls_today(user) -> int:
    """Actual (billable) extraction calls this user has made today."""
    return LlmExtractionCall.objects.filter(
        user=user, created_at__date=timezone.localdate()
    ).count()


def _extract_payload(facts: dict, document, regex_count: int, *, deduped: bool = False) -> dict:
    """Shape the JSON the Stage-0 page expects from an extraction (or a deduped no-op)."""
    text = document.extracted_text or ""
    return {
        "status": facts.get("status", "skipped"),
        "deduped": deduped,
        "manuscript_facts": facts,
        "metadata_count": len(facts.get("metadata", {})),
        "events_count": len(facts.get("events", [])),
        "regex_suggestions": regex_count,
        "page_count": document.page_count,
        "char_count": document.char_count,
        "word_count": len(text.split()),
    }


@require_POST
def extract_document(request, token):
    """Re-run extraction on the attached document and merge into the draft's facts.

    This re-runs *text* extraction from the stored bytes first (so a document
    captured before an extractor improvement picks it up without re-uploading),
    then the deterministic parsers and the model module.

    Two cost guards wrap the model call (accidental-spam protection):
      * dedupe — an unchanged, already-successfully-analysed paper returns the stored
        facts with no new call unless ``force=1`` is sent (the "re-run anyway" path);
      * a per-account daily cap (``WORKBENCH_LLM_DAILY_CAP``), staff exempt.
    """
    submission = _owned_submission(request, token)
    document = submission.documents.order_by("-created_at").first()
    if document is None:
        return JsonResponse({"error": "No document attached yet."}, status=400)

    force = request.POST.get("force") == "1"

    # Dedupe: this exact document was already analysed successfully. Re-running it
    # would burn a model call for (almost always) the identical result, so unless the
    # contributor explicitly forces it, just hand back what we already have.
    if (not force and document.sha256
            and document.sha256 == submission.manuscript_source_sha256):
        return JsonResponse(
            _extract_payload(submission.manuscript_facts or {}, document, 0, deduped=True)
        )

    # Refresh the plain text from the original bytes. Pasted text has no bytes
    # stored, so its extracted_text stands as-is.
    if document.file_bytes is not None:
        result = extract_text(
            document.kind, bytes(document.file_bytes), filename=document.original_name
        )
        document.extracted_text = result.text
        document.page_count = result.page_count
        document.char_count = result.char_count
        document.extraction_status = "failed" if result.error else "extracted"
        document.extraction_error = result.error
        document.save(
            update_fields=[
                "extracted_text", "page_count", "char_count",
                "extraction_status", "extraction_error",
            ]
        )

    if not document.extracted_text:
        return JsonResponse(
            {"error": "No extracted document text to analyse yet."}, status=400
        )

    # Daily cap — only when a real model call is imminent (assist on). Staff exempt;
    # cap of 0 disables it. Deduped no-ops above never reach here, so they don't count.
    cap = getattr(settings, "WORKBENCH_LLM_DAILY_CAP", 0)
    if llm_assist.is_enabled() and cap and not request.user.is_staff:
        if _llm_calls_today(request.user) >= cap:
            return JsonResponse(
                {"error": f"You’ve reached today’s limit of {cap} AI extractions. "
                          f"It resets tomorrow — you can still fill in the forms "
                          f"manually. Contact us if you need a higher limit."},
                status=429,
            )

    module_result = manuscript_extract.extract_facts(
        document.extracted_text, _already_have(submission), _option_choices()
    )
    status = module_result.get("status", "skipped")

    # Record only real, billable calls (ok/failed). "skipped" means the assist was off
    # or there was no text — no cost, so it neither logs nor counts against the cap.
    if status in ("ok", "failed"):
        LlmExtractionCall.objects.create(
            user=request.user, submission=submission, status=status
        )

    # Re-run the deterministic parsers and merge, preferring their numbers (§4).
    regex_meta = manuscript_regex.parse_all(document.extracted_text)
    metadata = merge_metadata(regex_meta, module_result.get("metadata", {}))

    facts = dict(submission.manuscript_facts or {})
    facts["status"] = status
    facts["metadata"] = metadata
    facts["events"] = module_result.get("events", [])
    for key in ("model", "generated_at", "usage"):
        if key in module_result:
            facts[key] = module_result[key]

    # Remember which document produced a *successful* extraction so a later re-run on
    # the unchanged paper is deduped away.
    if status == "ok":
        submission.manuscript_source_sha256 = document.sha256 or ""
    submission.manuscript_facts = facts
    submission.manuscript_extracted_at = timezone.now()
    submission.save(
        update_fields=[
            "manuscript_facts", "manuscript_extracted_at",
            "manuscript_source_sha256", "updated_at",
        ]
    )

    return JsonResponse(_extract_payload(facts, document, len(regex_meta)))


@require_GET
def document_text(request, token):
    """Serve the extracted plain text for the read-only document panel."""
    submission = _owned_submission(request, token)
    document = submission.documents.order_by("-created_at").first()
    text = document.extracted_text if document else ""
    # Plain text; the template escapes it when rendering into the panel.
    return HttpResponse(text, content_type="text/plain; charset=utf-8")


def _preview_of(parsed_content: dict) -> list[dict]:
    """A small, render-ready view of parsed sheets for the browser."""
    preview = []
    for sheet in parsed_content.get("sheets", []):
        preview.append(
            {
                "name": sheet["name"],
                "headers": sheet["headers"],
                "dtypes": sheet["dtypes"],
                "sample_rows": sheet["rows"][:_PREVIEW_ROWS],
                "n_rows": sheet["n_rows"],
                "n_cols": len(sheet["headers"]),
                "truncated": sheet["truncated"],
            }
        )
    return preview


@require_POST
def upload_data(request, token):
    """Parse an uploaded CSV/XLSX into the submission's ``parsed_content``.

    This is the file-upload *producer* of the source-agnostic parsed-content
    contract (see ``services/tabular.py``). A future figure-extraction feature
    would be a different producer writing the same structure.
    """
    submission = _owned_submission(request, token)

    upload = request.FILES.get("data")
    if upload is None:
        return JsonResponse({"error": "No data file provided."}, status=400)

    rejection = _upload_rejection(upload, submission.data_files.count())
    if rejection is not None:
        return rejection

    data = upload.read()
    kind = tabular.sniff_kind(upload.name or "", data[:8])
    if kind is None:
        return JsonResponse(
            {"error": "Unsupported file — please upload a CSV or Excel (.xlsx) file."},
            status=400,
        )

    result = tabular.parse_tabular(kind, data, filename=upload.name or "")
    total_rows = sum(s.n_rows for s in result.sheets)

    doc = UploadedDataFile.objects.create(
        submission=submission,
        kind=kind,
        original_name=upload.name or "",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        file_bytes=data,
        sheet_count=len(result.sheets),
        row_count=total_rows,
        parse_status="failed" if result.error else "parsed",
        parse_error=result.error,
    )

    if result.error:
        return JsonResponse(
            {
                "status": "failed",
                "data_file_id": doc.id,
                "error": result.error,
            },
            status=200,
        )

    submission.parsed_content = result.as_content()
    submission.save(update_fields=["parsed_content", "updated_at"])

    return JsonResponse(
        {
            "status": "parsed",
            "data_file_id": doc.id,
            "kind": kind,
            "sheet_count": len(result.sheets),
            "row_count": total_rows,
            "preview": _preview_of(submission.parsed_content),
        }
    )


# --- Stage 2: column mapping -------------------------------------------------

def _detect_measurement_sheet(sheets: list[dict]) -> int:
    """Best guess at which sheet holds the time-series measurements: the one whose
    columns most look like a date + N2O flux series, tie-broken by row count. Static
    sheets (soil/site, one row per plot) score lower and won't be picked."""
    best_i, best_score = 0, None
    for i, s in enumerate(sheets):
        sug = suggest.suggest_columns(
            s.get("headers", []), s.get("dtypes", []), fields=canonical.mappable_fields()
        )
        fields = {x["canonical_field"] for x in sug
                  if x["canonical_field"] and x["confidence"] >= _AUTO_APPLY_CONFIDENCE}
        score = ("date" in fields, "n2oflux" in fields, s.get("n_rows", 0))
        if best_score is None or score > best_score:
            best_i, best_score = i, score
    return best_i


def _measurement_sheet_index(submission: UploadSubmission) -> int | None:
    """Index of the sheet mapped as the measurement series — the contributor's saved
    choice if valid, else the auto-detected best guess."""
    sheets = (submission.parsed_content or {}).get("sheets", [])
    if not sheets:
        return None
    saved = (submission.structure or {}).get("measurement_sheet")
    if isinstance(saved, int) and 0 <= saved < len(sheets):
        return saved
    return _detect_measurement_sheet(sheets)


def _primary_sheet(submission: UploadSubmission) -> dict | None:
    """The measurement sheet whose columns are mapped to per-date canonical fields.
    Only one sheet feeds the time-series so every row shares a date axis; other sheets
    hold static (soil/site) data consumed on the Site & Soil steps."""
    sheets = (submission.parsed_content or {}).get("sheets", [])
    idx = _measurement_sheet_index(submission)
    return sheets[idx] if idx is not None else None


def _column_samples(sheet: dict, col_index: int, limit: int = _MAP_SAMPLES) -> list[str]:
    samples: list[str] = []
    for row in sheet.get("rows", []):
        if col_index < len(row):
            v = row[col_index]
            if v is not None and str(v).strip():
                samples.append(str(v))
        if len(samples) >= limit:
            break
    return samples


def mapping(request, token):
    """Render the column-mapping screen for the submission's primary sheet."""
    submission = _owned_submission(request, token)
    sheet = _primary_sheet(submission)
    if sheet is None:
        # No data uploaded yet — send them back to Stage 0.
        return redirect("submit-data")

    headers = sheet["headers"]
    dtypes = sheet.get("dtypes", [])
    # One sheet feeds the time-series (so every dated row shares an axis). If the
    # workbook has more, offer a switcher to pick which is the measurement sheet;
    # the others' columns are used on the Site & Soil steps, not dropped.
    all_sheets = (submission.parsed_content or {}).get("sheets", [])
    meas_index = _measurement_sheet_index(submission)
    sheet_choices = [{"index": i, "name": s.get("name") or f"Sheet {i + 1}"}
                     for i, s in enumerate(all_sheets)]
    other_sheets = [c["name"] for c in sheet_choices if c["index"] != meas_index]
    # Only suggest/allow mappable targets — derived fields (DOY, nitrogen type, …) are
    # computed by us, never hand-mapped, so they don't appear on this page.
    mappable = canonical.mappable_fields()
    mappable_ids = {f.id for f in mappable}
    suggestions = suggest.suggest_columns(headers, dtypes, fields=mappable)
    saved = submission.mapping or {}

    columns = []
    claimed: dict[str, str] = {}  # canonical_field -> header (one column per field)
    for i, name in enumerate(headers):
        sug = suggestions[i]
        saved_field = (saved.get(name) or {}).get("canonical_field")
        # Only honor a saved field if it's still a mappable target. A stale value
        # (e.g. from before a registry change) would otherwise mark the column
        # "mapped" with no field to render on, making it vanish from the page.
        if saved_field in mappable_ids:
            assigned = saved_field
        elif sug["canonical_field"] and sug["confidence"] >= _AUTO_APPLY_CONFIDENCE:
            assigned = sug["canonical_field"]
        else:
            assigned = None
        # One column per field. If this field is already taken (e.g. a stale save
        # mapped two columns to the same target), fall back to this column's own
        # suggestion if that's free, else leave it unmapped so it stays visible in
        # the pool rather than hiding behind the other column.
        if assigned and assigned in claimed:
            alt = sug["canonical_field"]
            if alt and alt not in claimed and sug["confidence"] >= _AUTO_APPLY_CONFIDENCE:
                assigned = alt
            else:
                assigned = None
        if assigned:
            claimed[assigned] = name
        columns.append(
            {
                "name": name,
                "dtype": dtypes[i] if i < len(dtypes) else "string",
                "samples": _column_samples(sheet, i),
                "suggestion": sug,
                "assigned": assigned,
                "unit_hint": units.extract_unit_hint(name),
                "saved_unit": (saved.get(name) or {}).get("unit"),
                "saved_date_format": (saved.get(name) or {}).get("date_format"),
            }
        )

    field_groups = [
        {
            "name": group,
            # Event fields (fertilisation, planting, tillage, harvest) needn't be columns
            # in the spreadsheet — they can be read from the paper or added on the Timeline
            # step — so the mapping UI marks this group optional/deferrable.
            "deferrable": group == "Events",
            "fields": [
                {
                    "id": f.id,
                    "label": f.label,
                    "unit": f.unit,
                    "dtype": f.dtype,
                    "required": f.required,
                }
                for f in mappable
                if f.group == group
            ],
        }
        for group in canonical.groups()
    ]
    field_groups = [g for g in field_groups if g["fields"]]  # drop now-empty groups
    required_ids = [f.id for f in mappable if f.required]

    return render(
        request,
        "workbench_mapping.html",
        {
            "submission": submission,
            "public_token": submission.public_token,
            "sheet_name": sheet["name"],
            "n_rows": sheet["n_rows"],
            "sheet_choices": sheet_choices,
            "measurement_index": meas_index,
            "other_sheets": other_sheets,
            "columns": columns,
            "field_groups": field_groups,
            "required_ids": required_ids,
            "unit_options": units.UNIT_OPTIONS,
            "date_formats": DATE_FORMATS,
            **_wizard_context(current_step=2, token=submission.public_token),
        },
    )


@require_POST
def save_mapping(request, token):
    """Persist the column → canonical-field mapping (autosaved from the UI).

    Each entry may be a bare field id or ``{"field": id, "unit": <str|null>}``.
    Stored as ``{header: {canonical_field, unit, source}}``.
    """
    submission = _owned_submission(request, token)
    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    incoming = payload.get("mapping", {})
    # Only mappable targets are accepted — derived fields (DOY, nitrogen type, management)
    # are never hand-mapped, even via a crafted request.
    valid_fields = {f.id for f in canonical.mappable_fields()}
    mapping_out: dict = {}
    for header, entry in incoming.items():
        if isinstance(entry, dict):
            field_id = entry.get("field") or entry.get("canonical_field")
            unit = entry.get("unit")
        else:
            field_id, unit = entry, None
        if field_id not in valid_fields:
            continue
        row = {"canonical_field": field_id, "source": "user"}
        if unit and unit in units.options_for(field_id):
            row["unit"] = unit
        # The date column carries the contributor's declared day/month order.
        if field_id == "date" and isinstance(entry, dict):
            fmt = entry.get("date_format")
            if fmt in _DATE_FORMAT_IDS:
                row["date_format"] = fmt
        mapping_out[header] = row

    submission.mapping = mapping_out
    submission.save(update_fields=["mapping", "updated_at"])
    return JsonResponse({"status": "saved", "mapped_count": len(mapping_out)})


@require_POST
def set_measurement_sheet(request, token):
    """Choose which parsed sheet is the time-series measurement sheet. Clears the
    column mapping (headers differ between sheets, so old assignments don't carry)."""
    submission = _owned_submission(request, token)
    try:
        idx = int(json.loads(request.body or "{}").get("index"))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid sheet index."}, status=400)
    sheets = (submission.parsed_content or {}).get("sheets", [])
    if not (0 <= idx < len(sheets)):
        return JsonResponse({"error": "Sheet index out of range."}, status=400)

    structure = submission.structure or {}
    if structure.get("measurement_sheet") != idx:
        structure["measurement_sheet"] = idx
        submission.structure = structure
        submission.mapping = {}  # a different sheet's columns can't reuse the old mapping
        submission.save(update_fields=["structure", "mapping", "updated_at"])
    return JsonResponse({"status": "saved", "measurement_sheet": idx})


# --- Stage 3: site & study details (Plane B) ---------------------------------

def _leftover_columns(submission: UploadSubmission, fields=None) -> list[dict]:
    """Candidate CSV columns for study variables (pH, bulk density, coordinates…),
    drawn from *every* sheet — static data commonly lives on a separate tab from the
    time-series. The measurement sheet contributes only columns not already mapped to
    a canonical field; other sheets contribute all their columns. Each entry carries a
    representative value (first non-null), a distinct-count (so the UI can warn when it
    varies by row), the source ``sheet`` name, and a unique ``key`` (sheet + header) so
    same-named columns on different sheets stay distinct.
    ``fields`` defaults to the global site fields; the Soil stage passes the
    treatment-level registry instead.
    """
    if fields is None:
        fields = site_fields.global_fields()
    sheets = (submission.parsed_content or {}).get("sheets", [])
    if not sheets:
        return []
    meas_idx = _measurement_sheet_index(submission)
    mapped = set(submission.mapping or {})

    out = []
    for si, sheet in enumerate(sheets):
        headers = sheet.get("headers", [])
        dtypes = sheet.get("dtypes", [])
        # On the measurement sheet, hide columns already claimed by the mapping step;
        # other sheets are purely static so all their columns are offered.
        idxs = [i for i, h in enumerate(headers)
                if not (si == meas_idx and h in mapped)]
        if not idxs:
            continue
        sug = suggest.suggest_columns(
            [headers[i] for i in idxs],
            [dtypes[i] if i < len(dtypes) else None for i in idxs],
            fields=fields,
        )
        sheet_name = sheet.get("name") or f"Sheet {si + 1}"
        for pos, i in enumerate(idxs):
            values = [row[i] for row in sheet.get("rows", []) if i < len(row)
                      and row[i] is not None and str(row[i]).strip()]
            out.append(
                {
                    "key": f"{si}::{headers[i]}",
                    "name": headers[i],
                    "sheet": sheet_name,
                    "sheet_index": si,
                    "dtype": (dtypes[i] if i < len(dtypes) else None) or "string",
                    "samples": _column_samples(sheet, i),
                    "value": values[0] if values else None,
                    "distinct": len(set(str(v) for v in values)),
                    "suggestion": sug[pos],
                }
            )
    return out


def _field_dict(f) -> dict:
    return {
        "id": f.id,
        "label": f.label,
        "unit": f.unit,
        "dtype": f.dtype,
        "required": f.required,
        "options": list(f.options),
    }


def _grouped_fields(fields, group_order) -> list[dict]:
    return [
        {
            "name": group,
            "fields": [_field_dict(f) for f in fields if f.group == group],
        }
        for group in group_order
    ]


def _initial_site_values(submission: UploadSubmission, fields) -> dict:
    """Per-field initial value + provenance: saved answer > manuscript fact > empty."""
    saved = submission.metadata_values or {}
    facts = (submission.manuscript_facts or {}).get("metadata", {})
    values: dict = {}
    for f in fields:
        if f.id in saved and isinstance(saved[f.id], dict):
            # Tag already-persisted answers so the form can treat them as confirmed
            # (the Site stage can't otherwise tell a saved value from a paper prefill).
            values[f.id] = {**saved[f.id], "_saved": True}
        elif f.id in facts:
            m = facts[f.id]
            values[f.id] = {
                "value": m.get("value"),
                "source": "document",
                "source_quote": m.get("source_quote"),
                "confidence": m.get("confidence"),
                "unit": m.get("unit"),
            }
    return values


def site_info(request, token):
    """Render the global (one-per-study) Plane B details form."""
    submission = _owned_submission(request, token)
    fields = site_fields.global_fields()
    required_ids = [f.id for f in fields if f.required]
    return render(
        request,
        "workbench_site.html",
        {
            "submission": submission,
            "public_token": submission.public_token,
            "field_groups": _grouped_fields(fields, site_fields.global_groups()),
            "initial_values": _initial_site_values(submission, fields),
            "leftover_columns": _leftover_columns(submission),
            "required_ids": required_ids,
            **_wizard_context(current_step=3, token=submission.public_token),
        },
    )


@require_POST
def save_site(request, token):
    """Persist the global site/study details (autosaved from the UI)."""
    submission = _owned_submission(request, token)
    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    incoming = payload.get("values", {})
    global_fields = site_fields.global_fields()
    valid = {f.id for f in global_fields}
    out: dict = {}
    for field_id, entry in incoming.items():
        if field_id not in valid or not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if value in (None, ""):
            continue
        clean = {"value": value, "source": entry.get("source", "user")}
        for key in ("column", "columnKey", "sheet", "source_quote"):
            if entry.get(key):
                clean[key] = entry[key]
        out[field_id] = clean

    submission.metadata_values = out
    submission.save(update_fields=["metadata_values", "updated_at"])
    filled_required = [
        fid for fid in (f.id for f in global_fields if f.required) if fid in out
    ]
    return JsonResponse(
        {"status": "saved", "filled_count": len(out), "required_filled": filled_required}
    )


# --- Stage 4: soil & management (global or per-treatment) --------------------

def _treatment_column(submission: UploadSubmission) -> str | None:
    """The header a contributor mapped to the ``treatment`` identifier, if any."""
    for header, entry in (submission.mapping or {}).items():
        if isinstance(entry, dict) and entry.get("canonical_field") == "treatment":
            return header
    return None


def _distinct_column_values(sheet: dict, col_index: int) -> list[str]:
    """Distinct non-empty values of a column, in first-seen order."""
    seen: list[str] = []
    for row in sheet.get("rows", []):
        if col_index < len(row):
            v = row[col_index]
            if v is not None and str(v).strip():
                s = str(v).strip()
                if s not in seen:
                    seen.append(s)
    return seen


def _detected_treatments(submission: UploadSubmission, trt_col: str | None) -> list[str]:
    sheet = _primary_sheet(submission)
    if sheet is None or not trt_col or trt_col not in sheet["headers"]:
        return []
    return _distinct_column_values(sheet, sheet["headers"].index(trt_col))


# Treatment-level fields whose dropdown suggestions come from values already in the
# live database, so contributors reuse the existing controlled vocabulary rather
# than inventing new spellings. field_id -> Treatment model column.
_SUGGEST_FROM_DB = {
    "management": "Management",
    "flux_instrument": "FluxInstrument",
}


def _distinct_model_values(model, model_field: str) -> list[str]:
    """Distinct non-empty values of a model column, most-common first.

    Best-effort: any DB hiccup degrades to no suggestions (the field stays a free
    text input) rather than breaking the page.
    """
    from django.db.models import Count
    try:
        rows = (
            model.objects
            .exclude(**{f"{model_field}__isnull": True})
            .exclude(**{model_field: ""})
            .values(model_field)
            .annotate(n=Count(model_field))
            # Secondary sort by the value itself: ties in the count would otherwise
            # come back in an arbitrary order, which changes the (cached) LLM system
            # prompt call-to-call and busts prompt caching. Keep it deterministic.
            .order_by("-n", model_field)
        )
        seen: list[str] = []
        for r in rows:
            v = (r[model_field] or "").strip()
            if v and v not in seen:
                seen.append(v)
        return seen
    except Exception:  # noqa: BLE001 — suggestions are a nicety, never a hard dep
        logger.exception("could not load existing %s.%s values", model.__name__, model_field)
        return []


def _existing_values(model_field: str) -> list[str]:
    """Distinct non-empty values of a Treatment column, most-common first."""
    return _distinct_model_values(Treatment, model_field)


def _with_db_options(field_groups: list[dict]) -> list[dict]:
    """Turn configured fields into strict dropdowns of existing DB values."""
    for group in field_groups:
        for f in group["fields"]:
            col = _SUGGEST_FROM_DB.get(f["id"])
            if col:
                f["options"] = _existing_values(col)
    return field_groups


def _option_choices() -> dict:
    """Controlled vocabularies for constrained metadata fields, for the LLM to pick
    from (texture = the fixed USDA list; management / flux = the same existing DB
    values the Soil & Management dropdowns are built from)."""
    tex = site_fields.get("texture_class")
    choices = {"texture_class": list(tex.options) if tex else []}
    for fid, col in _SUGGEST_FROM_DB.items():
        choices[fid] = _existing_values(col)
    return choices


def _paper_facts(submission: UploadSubmission, fields) -> dict:
    """Field id -> {value, source_quote} from the manuscript module, for prefill."""
    facts = (submission.manuscript_facts or {}).get("metadata", {})
    out: dict = {}
    for f in fields:
        if f.id in facts:
            m = facts[f.id]
            if m.get("value") not in (None, ""):
                out[f.id] = {"value": m.get("value"), "source_quote": m.get("source_quote")}
    return out


def _sheet_treatment_column_idx(sheet: dict, detected: list[str]) -> int | None:
    """Index of the column in ``sheet`` that best identifies treatments — the one whose
    distinct values overlap the detected treatment set most. Lets a static soil sheet be
    keyed by its own treatment column, which may differ from the measurement sheet's."""
    dset = {str(t).strip() for t in detected}
    if not dset:
        return None
    best_idx, best_overlap = None, 0
    for i, _ in enumerate(sheet.get("headers", [])):
        vals = {str(r[i]).strip() for r in sheet.get("rows", [])
                if i < len(r) and r[i] is not None and str(r[i]).strip()}
        overlap = len(vals & dset)
        if overlap > best_overlap:
            best_idx, best_overlap = i, overlap
    return best_idx


def _column_treatment_values(sheet: dict, col_idx: int, trt_idx: int,
                             detected: list[str]) -> dict:
    """{treatment: first non-null value of ``col_idx``} keyed by the sheet's own
    treatment column — so per-treatment soil fields fill from the matching plot rows."""
    out: dict = {}
    want = {str(t).strip() for t in detected}
    for row in sheet.get("rows", []):
        if trt_idx >= len(row) or col_idx >= len(row):
            continue
        t = str(row[trt_idx]).strip()
        if t in want and t not in out:
            v = row[col_idx]
            if v is not None and str(v).strip():
                out[t] = v
    return out


def _soil_leftover_columns(submission: UploadSubmission) -> list[dict]:
    """Leftover columns for the Soil step: all sheets' candidate columns matched to the
    treatment-level soil fields, each augmented with per-treatment values (first value
    per treatment, keyed by that column's own sheet's treatment column)."""
    cols = _leftover_columns(submission, fields=site_fields.treatment_fields())
    sheets = (submission.parsed_content or {}).get("sheets", [])
    detected = _detected_treatments(submission, _treatment_column(submission))
    trt_idx_cache: dict = {}
    for c in cols:
        si = c["sheet_index"]
        sheet = sheets[si]
        if si not in trt_idx_cache:
            trt_idx_cache[si] = _sheet_treatment_column_idx(sheet, detected)
        trt_idx = trt_idx_cache[si]
        col_idx = sheet["headers"].index(c["name"])
        c["treatment_values"] = (
            _column_treatment_values(sheet, col_idx, trt_idx, detected)
            if trt_idx is not None and trt_idx != col_idx else {}
        )
    return cols


def soil_mgmt(request, token):
    """Render the Soil & Management stage (per-group global vs per-treatment)."""
    submission = _owned_submission(request, token)
    if _primary_sheet(submission) is None:
        return redirect("submit-data")

    fields = site_fields.treatment_fields()
    trt_col = _treatment_column(submission)
    detected = _detected_treatments(submission, trt_col)
    saved = submission.treatment_details or {}

    return render(
        request,
        "workbench_soil.html",
        {
            "submission": submission,
            "public_token": submission.public_token,
            "field_groups": _with_db_options(
                _grouped_fields(fields, site_fields.treatment_groups())
            ),
            "treatment_groups": site_fields.treatment_groups(),
            "treatment_column": trt_col,
            "detected_treatments": detected,
            "saved_details": saved,
            "leftover_columns": _soil_leftover_columns(submission),
            "paper_facts": _paper_facts(submission, fields),
            "required_ids": [f.id for f in fields if f.required],
            **_wizard_context(current_step=4, token=submission.public_token),
        },
    )


@require_POST
def save_soil_mgmt(request, token):
    """Persist the soil/management answers (autosaved from the UI)."""
    submission = _owned_submission(request, token)
    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    valid = {f.id for f in site_fields.treatment_fields()}
    valid_groups = set(site_fields.treatment_groups())

    # Treatment identity is owned by the mapped data column — those distinct values key
    # the RawMeasurementTreatment rows and the promoted Treatment FKs. The client cannot
    # rename/add/remove treatments here; doing so would orphan the annotations from the
    # data. We always take the detected set and ignore any submitted names.
    treatments = _detected_treatments(submission, _treatment_column(submission))

    # Optional human-readable name per treatment (keyed by the identifier). The identifier
    # stays the join key; this label is what gets written to Treatment.TreatmentName, so
    # e.g. "CST-U" → "Conservation tillage + urea". Falls back to the identifier if unset.
    names: dict = {}
    for key, label in (payload.get("names") or {}).items():
        if key in treatments and str(label).strip():
            names[key] = str(label).strip()

    # Short free-text description per treatment (rotation, tillage, N rate…).
    descriptions: dict = {}
    for name, desc in (payload.get("descriptions") or {}).items():
        if name in treatments and str(desc).strip():
            descriptions[name] = str(desc).strip()

    scopes: dict = {}
    for group, mode in (payload.get("scopes") or {}).items():
        if group in valid_groups and mode in ("global", "per_treatment"):
            scopes[group] = mode

    def _clean(entry) -> dict | None:
        if not isinstance(entry, dict):
            return None
        value = entry.get("value")
        if value in (None, ""):
            return None
        clean = {"value": value, "source": entry.get("source", "user")}
        for key in ("column", "columnKey", "sheet", "source_quote"):
            if entry.get(key):
                clean[key] = entry[key]
        return clean

    global_out: dict = {}
    for fid, entry in (payload.get("global") or {}).items():
        if fid in valid:
            c = _clean(entry)
            if c:
                global_out[fid] = c

    per_out: dict = {}
    for tname, entries in (payload.get("per_treatment") or {}).items():
        if tname not in treatments or not isinstance(entries, dict):
            continue
        row: dict = {}
        for fid, entry in entries.items():
            if fid in valid:
                c = _clean(entry)
                if c:
                    row[fid] = c
        if row:
            per_out[tname] = row

    submission.treatment_details = {
        "treatments": treatments,
        "names": names,
        "descriptions": descriptions,
        "treatment_column": _treatment_column(submission),
        "scopes": scopes,
        "global": global_out,
        "per_treatment": per_out,
    }
    submission.save(update_fields=["treatment_details", "updated_at"])
    return JsonResponse(
        {
            "status": "saved",
            "treatments": len(treatments),
            "global_filled": len(global_out),
            "per_treatment_filled": sum(len(v) for v in per_out.values()),
        }
    )


# --- Stage 5: timeline of events (global or per-treatment) -------------------

def _iso_date(value) -> str | None:
    """Canonical YYYY-MM-DD if ``value`` is a true calendar date, else None.

    Event timing is often vague in papers ("late July", "at planting"). Only a real
    date belongs in the database; a broad string is kept separately as a manual-entry
    hint, never stored as the date.
    """
    from datetime import date as _date
    if value in (None, ""):
        return None
    s = str(value).strip()
    for cand in (s, s[:10]):  # tolerate a trailing time part on an ISO datetime
        try:
            return _date.fromisoformat(cand).isoformat()
        except ValueError:
            continue
    return None


def _event_option_choices() -> dict:
    """Controlled vocabularies for event dropdowns, from live DB data.

    Fertiliser *type* is a broad classification the contributor picks from the same
    ``NitrogenType`` values already in the database — so the timeline reuses the
    existing vocabulary rather than inventing spellings.
    """
    return {"nitrogen_type": _distinct_model_values(RawMeasurementTreatment, "nitrogentype")}


def _timeline_treatments(submission: UploadSubmission) -> list[str]:
    """Treatments to offer per-event, reusing what the Soil stage established."""
    saved = [
        str(t).strip()
        for t in (submission.treatment_details or {}).get("treatments", [])
        if str(t).strip()
    ]
    if saved:
        return saved
    return _detected_treatments(submission, _treatment_column(submission))


def _paper_events(submission: UploadSubmission, options: dict) -> dict:
    """Group the module's extracted events by timeline section, as pre-fill cards.

    Returns ``{section_id: [ {fields, source, source_quote, confidence}, ... ]}``.
    A fertiliser *type* is resolved to the DB vocabulary (off-list → left blank, so a
    strict dropdown never shows an unlisted value).
    """
    by_contract = {et.contract: et for et in event_fields.types()}
    out: dict = {et.id: [] for et in event_fields.types()}
    nvocab = options.get("nitrogen_type", [])

    for ev in (submission.manuscript_facts or {}).get("events", []):
        et = by_contract.get(ev.get("type"))
        if et is None:
            continue
        parts = {f.get("key"): f for f in ev.get("fields", []) if isinstance(f, dict)}
        fields: dict = {}
        # Only a real calendar date fills the (true-date) field; the paper's as-stated
        # timing rides along as a hint to help the contributor pick the actual day.
        real_date = _iso_date(ev.get("date_iso"))
        if real_date:
            fields["date"] = real_date
        date_hint = (ev.get("date_text") or "").strip()
        if not date_hint and not real_date:
            date_hint = (ev.get("date_iso") or "").strip()  # e.g. "2018-07" (not a full date)
        if et.id == "fertilisation":
            amt = parts.get("amount") or {}
            if amt.get("value") is not None:
                fields["n_amount"] = amt.get("value")
                if amt.get("unit"):
                    fields["n_unit"] = amt.get("unit")
            form = (parts.get("input_form") or {}).get("value")
            if form:
                fields["form"] = form
            raw_type = (parts.get("input_source") or {}).get("value")
            if raw_type and nvocab:
                canon = manuscript_extract._resolve_option(raw_type, nvocab)
                if canon:
                    fields["type"] = canon
        elif et.id == "planting":
            crop = (parts.get("crop") or {}).get("value")
            if crop:
                fields["crop"] = crop
        elif et.id == "tillage":
            inst = (parts.get("tillage_instrument") or {}).get("value")
            if inst:
                fields["instrument"] = inst
        if fields or date_hint:
            rec = {
                "fields": fields,
                "source": "document",
                "source_quote": ev.get("source_quote"),
                "confidence": ev.get("confidence"),
            }
            if date_hint and date_hint != real_date:
                rec["date_hint"] = date_hint
            out[et.id].append(rec)
    return out


def timeline(request, token):
    """Render the Timeline stage (dated events, per-type global vs per-treatment)."""
    submission = _owned_submission(request, token)
    if _primary_sheet(submission) is None:
        return redirect("submit-data")

    options = _event_option_choices()
    saved = submission.events if isinstance(submission.events, dict) else {}
    # Event sections already supplied as mapped columns are shown as done (no prompt).
    provided = _column_provided_event_sections(submission)
    seeded = _column_seeded_fertilisation(submission)
    event_types = event_fields.for_template(options)
    for et in event_types:
        et["column_provided"] = et["id"] in provided
        et["column_sources"] = provided.get(et["id"], [])
        # Partial fertilisation: one paired column mapped, the other to be completed here.
        et["column_seeded"] = seeded if (seeded and et["id"] == "fertilisation") else None
    return render(
        request,
        "workbench_timeline.html",
        {
            "submission": submission,
            "public_token": submission.public_token,
            "event_types": event_types,
            "treatments": _timeline_treatments(submission),
            "paper_events": _paper_events(submission, options),
            "saved_events": saved,
            **_wizard_context(current_step=5, token=submission.public_token),
        },
    )


@require_POST
def save_timeline(request, token):
    """Persist the timeline of events (autosaved from the UI).

    Shape stored on ``submission.events``::

        {scopes: {<section>: "global"|"per_treatment"},
         global: {<section>: [ {fields, source, source_quote?}, ... ]},
         per_treatment: {<treatment>: {<section>: [ ... ]}}}
    """
    submission = _owned_submission(request, token)
    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    # Sections whose data comes from a mapped column are owned by the data, not the
    # Timeline — never persist manual events for them (prevents contradiction).
    provided = set(_column_provided_event_sections(submission))
    section_ids = [et.id for et in event_fields.types() if et.id not in provided]
    treatments = set(_timeline_treatments(submission))

    def _clean_event(ev) -> dict | None:
        if not isinstance(ev, dict):
            return None
        raw = ev.get("fields") or {}
        allowed = _clean_event.allowed  # bound per section below
        fields: dict = {}
        for k, v in raw.items():
            if k not in allowed or v in (None, ""):
                continue
            if k == "date":
                # Store only a genuine calendar date; drop vague timing strings.
                iso = _iso_date(v)
                if iso is None:
                    continue
                fields[k] = iso
            else:
                fields[k] = v
        hint = str(ev.get("date_hint") or "").strip()
        if not fields and not hint:
            return None
        out = {"fields": fields, "source": ev.get("source", "user")}
        if ev.get("source_quote"):
            out["source_quote"] = ev["source_quote"]
        if ev.get("confidence") is not None:
            out["confidence"] = ev["confidence"]
        if hint:
            out["date_hint"] = hint  # manual-entry aid, not a stored date
        return out

    def _clean_list(lst, section) -> list:
        _clean_event.allowed = event_fields.allowed_keys(section)
        cleaned = [_clean_event(ev) for ev in (lst or [])]
        return [e for e in cleaned if e]

    scopes: dict = {}
    for section, mode in (payload.get("scopes") or {}).items():
        if section in section_ids and mode in ("global", "per_treatment"):
            scopes[section] = mode

    global_out: dict = {}
    for section, lst in (payload.get("global") or {}).items():
        if section in section_ids:
            evs = _clean_list(lst, section)
            if evs:
                global_out[section] = evs

    per_out: dict = {}
    for tname, sections in (payload.get("per_treatment") or {}).items():
        if tname not in treatments or not isinstance(sections, dict):
            continue
        row: dict = {}
        for section, lst in sections.items():
            if section in section_ids:
                evs = _clean_list(lst, section)
                if evs:
                    row[section] = evs
        if row:
            per_out[tname] = row

    submission.events = {"scopes": scopes, "global": global_out, "per_treatment": per_out}
    submission.save(update_fields=["events", "updated_at"])
    total = sum(len(v) for v in global_out.values()) + sum(
        len(v) for sec in per_out.values() for v in sec.values()
    )
    return JsonResponse({"status": "saved", "event_count": total})


# --- Stage 6: review (read-only tabular + graphical preview) -----------------

# Plane-A numeric columns surfaced in the review graphs (id must match a canonical
# field). The template's client-side config assigns each to a pane/axis.
_REVIEW_SERIES_FIELDS = (
    "n2oflux", "nitrogenapplied", "irrigationapplied",
    "vwc", "wfps", "soilt", "airt", "precip",
    "soilnh4", "soilno3",
)

# Explicit, contributor-selectable interpretations for the mapped date column. Picking
# one removes the MM/DD vs DD/MM ambiguity: we then parse *strictly* to that order and
# flag any value that doesn't fit, rather than silently guessing a wrong day.
DATE_FORMATS = [
    {"id": "iso", "label": "YYYY-MM-DD (ISO 8601)"},
    {"id": "mdy", "label": "MM/DD/YYYY (US)"},
    {"id": "dmy", "label": "DD/MM/YYYY (day first)"},
    {"id": "ymd", "label": "YYYY/MM/DD"},
]
_DATE_FORMAT_IDS = {f["id"] for f in DATE_FORMATS}

# Date part per declared format. A time-of-day suffix (below) is appended to each, so a
# value like "07/04/2018 13:45:00" parses too — used for time-weighted daily averaging of
# high-resolution data, and simply stripped to the date when the data is daily.
_DATE_FORMAT_BASES = {
    "iso": ("%Y-%m-%d",),
    "mdy": ("%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%m/%d/%y"),
    "dmy": ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"),
    "ymd": ("%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d"),
}
# Tolerant guess when no explicit format is declared ("auto"). Date-only; time added below.
_AUTO_DATE_BASES = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y",
    "%d-%b-%Y", "%d %b %Y", "%b %d %Y", "%m/%d/%y",
)
# Time-of-day variants tried after each date base (datetime first, bare date last).
_TIME_SUFFIXES = ("T%H:%M:%S", " %H:%M:%S", "T%H:%M", " %H:%M", "")


def _parse_datetime(value, fmt=None):
    """A ``datetime`` for a date/datetime value (time defaults to midnight), or None.

    Honours an explicit day/month order when ``fmt`` is given (strict — a non-conforming
    value returns None, surfaced as an error), else a tolerant guess. A time-of-day part
    is parsed when present so high-resolution data can be time-weighted.
    """
    import datetime as _dt

    if value in (None, ""):
        return None
    s = str(value).strip()
    # ISO 8601 fast path (covers "…T12:30:00", fractional seconds, offsets, etc.).
    if fmt in (None, "iso"):
        try:
            return _dt.datetime.fromisoformat(s)
        except ValueError:
            pass
    bases = _DATE_FORMAT_BASES.get(fmt) or _AUTO_DATE_BASES
    for base in bases:
        for suf in _TIME_SUFFIXES:
            try:
                return _dt.datetime.strptime(s, base + suf)
            except ValueError:
                continue
    return None


def _parse_date(value, fmt=None):
    """The calendar date of a date/datetime value (time stripped), or None."""
    dt = _parse_datetime(value, fmt)
    return dt.date() if dt else None


def _time_weighted_mean(pairs: list) -> float:
    """Trapezoidal (linear-interpolation) time-weighted mean of ``[(datetime, value), …]``.
    Falls back to a plain mean when the points don't span any time (e.g. date-only data)."""
    pts = sorted(pairs, key=lambda p: p[0])
    if len(pts) == 1:
        return pts[0][1]
    span = (pts[-1][0] - pts[0][0]).total_seconds()
    if span <= 0:
        return sum(v for _, v in pts) / len(pts)
    area = 0.0
    for a, b in zip(pts, pts[1:]):
        area += (a[1] + b[1]) / 2 * (b[0] - a[0]).total_seconds()
    return area / span


def _submission_date_format(submission: UploadSubmission) -> str | None:
    """The contributor-declared date format on the mapped date column (or None = auto)."""
    for entry in (submission.mapping or {}).values():
        if isinstance(entry, dict) and entry.get("canonical_field") == "date":
            fmt = entry.get("date_format")
            return fmt if fmt in _DATE_FORMAT_IDS else None
    return None


def _to_millis(value) -> int | None:
    """Epoch-ms at UTC midnight for a date-ish value (for the Highcharts datetime
    axis), or None if it isn't parseable."""
    import datetime as _dt

    d = _parse_date(value)
    if d is None:
        return None
    return int(_dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc).timestamp() * 1000)


def _fmt_cell(value) -> str:
    """Compact display for a table cell: trim float noise, leave strings/ints alone."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return f"{value:.4g}"
    return str(value)


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _mapped_field_columns(submission: UploadSubmission) -> dict:
    """canonical_field_id -> column index in the primary sheet, from the mapping."""
    sheet = _primary_sheet(submission)
    if sheet is None:
        return {}
    headers = sheet["headers"]
    out: dict = {}
    for header, entry in (submission.mapping or {}).items():
        if not isinstance(entry, dict):
            continue
        fid = entry.get("canonical_field")
        if fid and header in headers:
            out[fid] = headers.index(header)
    return out


def _event_marker(section: str, fields: dict) -> tuple[str, str]:
    """(label, kind) for a dated timeline event, for the graph's vertical markers."""
    if section == "fertilisation":
        amt = fields.get("n_amount")
        unit = fields.get("n_unit") or "kg N ha⁻¹"
        # A fertiliser event is a one-time application, not an annual rate — drop any
        # per-year part of the unit (e.g. "kg N ha⁻¹ year⁻¹" → "kg N ha⁻¹").
        unit = " ".join(
            tok for tok in unit.split()
            if "year" not in tok.lower() and "yr" not in tok.lower()
        ).strip()
        label = f"Fertiliser {amt} {unit}".strip() if amt not in (None, "") else "Fertiliser"
        return label, "fert"
    if section == "planting":
        crop = fields.get("crop")
        return (f"Planted {crop}" if crop else "Planting"), "plant"
    if section == "harvest":
        return "Harvest", "harvest"
    if section == "tillage":
        inst = fields.get("instrument")
        return (f"Tillage — {inst}" if inst else "Tillage"), "tillage"
    return section.title(), "other"


def _review_events(submission: UploadSubmission, treatment: str) -> list[dict]:
    """Dated timeline events applying to ``treatment`` (respecting each type's
    global/per-treatment scope), as ``{ms, label, kind}`` markers."""
    ev = submission.events if isinstance(submission.events, dict) else {}
    scopes = ev.get("scopes", {})
    glob = ev.get("global", {})
    per = (ev.get("per_treatment", {}) or {}).get(treatment, {})
    provided = set(_column_provided_event_sections(submission))
    out: list[dict] = []
    for section in (et.id for et in event_fields.types()):
        if section in provided:
            continue  # the mapped column is the single source for this section
        mode = scopes.get(section, "global")
        lst = per.get(section, []) if mode == "per_treatment" else glob.get(section, [])
        for e in lst or []:
            f = (e or {}).get("fields", {}) or {}
            ms = _to_millis(f.get("date"))
            if ms is None:
                continue
            label, kind = _event_marker(section, f)
            out.append({"ms": ms, "label": label, "kind": kind})
    out.sort(key=lambda m: m["ms"])
    return out


def _treatment_scope(submission: UploadSubmission, group: str) -> str:
    return (submission.treatment_details or {}).get("scopes", {}).get(group, "global")


def _treatment_group_items(submission: UploadSubmission, group: str, treatment) -> list[dict]:
    """Every field of a Treatment group (Soil / Crop & management) for ``treatment``,
    including blanks (marked ``empty``) so a reviewer sees exactly what is and isn't set.
    Values use the shared resolver, so they match what promotion will write."""
    items: list[dict] = []
    for f in site_fields.treatment_fields():
        if f.group != group:
            continue
        val = _resolved_treatment_value(submission, treatment, f.id) if treatment else None
        items.append({
            "label": f.label,
            "value": None if val in (None, "") else val,
            "unit": f.unit,
            "empty": val in (None, ""),
        })
    return items


def _global_treatment_groups(submission: UploadSubmission) -> list[dict]:
    """Treatment groups whose scope is *global* — one value for the whole study, so they
    belong in the study-wide panel rather than repeated under each treatment."""
    treatments = _timeline_treatments(submission)
    any_t = treatments[0] if treatments else None
    return [
        # Suffix makes it unambiguous in the study-wide panel that this Treatment group
        # applies to every treatment (vs. the per-treatment groups under the graph).
        {"name": f"{g} — all treatments", "items": _treatment_group_items(submission, g, any_t)}
        for g in site_fields.treatment_groups()
        if _treatment_scope(submission, g) == "global"
    ]


def _per_treatment_groups(submission: UploadSubmission, treatment: str) -> list[dict]:
    """Treatment groups whose scope is *per-treatment* — shown in the treatment switcher,
    since they can differ between treatments."""
    return [
        {"name": g, "items": _treatment_group_items(submission, g, treatment)}
        for g in site_fields.treatment_groups()
        if _treatment_scope(submission, g) != "global"
    ]


def _global_info(submission: UploadSubmission) -> list[dict]:
    """Every global site/study field (Stage 3), grouped for the left-hand panel.

    A review page should surface the whole record, so unfilled fields are included
    too (marked ``empty``) — the contributor sees exactly what is and isn't captured.
    Values are the same merged view the Site step shows: a saved answer wins, else the
    value the manuscript extractor suggested (so document-filled fields aren't blank
    here just because the contributor never re-typed them).
    """
    fields = site_fields.global_fields()
    merged = _initial_site_values(submission, fields)
    groups: list[dict] = []
    for gname in site_fields.global_groups():
        items = []
        for f in fields:
            if f.group != gname:
                continue
            entry = merged.get(f.id)
            filled = isinstance(entry, dict) and entry.get("value") not in (None, "")
            items.append({
                "label": f.label,
                "value": entry["value"] if filled else None,
                "unit": f.unit,
                "source": entry.get("source") if filled else None,
                "dtype": f.dtype,
                "empty": not filled,
            })
        if items:
            groups.append({"name": gname, "items": items})
    return groups


# timeline section -> {RawMeasurementTreatment row field: event field key}. Paper-derived
# events populate these per-date columns, so a management event that lands on a day with no
# measurement still gets a row (and a value) instead of being stranded. A None key means a
# presence flag (harvest/tillage → 1).
_EVENT_TO_ROW = {
    "fertilisation": {"nitrogenapplied": "n_amount", "nitrogenform": "form", "nitrogentype": "type"},
    "planting": {"plantedcrop": "crop"},
    "harvest": {"harvest": None},
    "tillage": {"tillage": None},
}


# Fertilisation's two paired columns: an application needs BOTH a rate and a form.
_FERT_FIELD_COLS = {"nitrogenapplied": "n_amount", "nitrogenform": "form"}


def _mapping_fid_to_header(submission: UploadSubmission) -> dict:
    out: dict = {}
    for header, entry in (submission.mapping or {}).items():
        if isinstance(entry, dict):
            fid = entry.get("canonical_field")
            if fid:
                out.setdefault(fid, header)
    return out


def _column_provided_event_sections(submission: UploadSubmission) -> dict:
    """timeline_section_id -> [source column headers] for event types *fully* supplied by
    mapped columns — shown as done, never prompted (the column is the single source).

    Fertilisation is special: an application needs both an N rate and an N form, so it's
    only fully provided when BOTH columns are mapped. With just one, it's *partially*
    provided — the Timeline seeds events from that column for the contributor to complete
    the missing field (see ``_column_seeded_fertilisation``)."""
    fid_to_header = _mapping_fid_to_header(submission)
    provided: dict = {}
    for section, rowmap in _EVENT_TO_ROW.items():
        headers = [fid_to_header[fid] for fid in rowmap if fid in fid_to_header]
        if not headers:
            continue
        if section == "fertilisation" and not all(
            fid in fid_to_header for fid in _FERT_FIELD_COLS
        ):
            continue  # only one of the pair → partial, handled by seeded events
        provided[section] = headers
    return provided


def _column_seeded_fertilisation(submission: UploadSubmission) -> dict | None:
    """When exactly one of fertilisation's paired columns (N applied / N form) is mapped,
    build per-treatment seed events from that column's non-zero/non-null dates, with the
    mapped field filled and the other blank — so the contributor supplies the missing half
    of every application. Returns None when neither or both columns are mapped."""
    fid_to_header = _mapping_fid_to_header(submission)
    mapped = {fid: ev_key for fid, ev_key in _FERT_FIELD_COLS.items() if fid in fid_to_header}
    if len(mapped) != 1:
        return None  # neither → normal manual entry; both → fully provided

    fid, ev_key = next(iter(mapped.items()))
    header = fid_to_header[fid]
    sheet = _primary_sheet(submission)
    if sheet is None or header not in sheet["headers"]:
        return None
    ci = sheet["headers"].index(header)
    colmap = _mapped_field_columns(submission)
    date_idx = colmap.get("date")
    trt_col = _treatment_column(submission)
    trt_idx = sheet["headers"].index(trt_col) if trt_col in sheet["headers"] else None
    date_fmt = _submission_date_format(submission)
    treatments = _detected_treatments(submission, trt_col) or ["All data"]
    unit = _column_units(submission).get("nitrogenapplied") if ev_key == "n_amount" else None

    per: dict = {t: {} for t in treatments}   # treatment -> {date_iso: value} (first per date)
    for row in sheet.get("rows", []):
        raw = row[ci] if ci < len(row) else None
        if raw in (None, ""):
            continue
        if ev_key == "n_amount":
            num = _to_float(raw)
            if num is None or num == 0:
                continue          # only real applications, not the 0 / blank days
            value = raw
        else:
            value = str(raw).strip()
            if not value:
                continue
        dt = _parse_datetime(
            row[date_idx] if date_idx is not None and date_idx < len(row) else None, date_fmt
        )
        if dt is None:
            continue
        tname = (str(row[trt_idx]).strip()
                 if trt_idx is not None and trt_idx < len(row) else treatments[0])
        if tname in per:
            per[tname].setdefault(dt.date().isoformat(), value)

    per_events: dict = {}
    for t, dvals in per.items():
        evs = []
        for diso in sorted(dvals):
            fields = {"date": diso, ev_key: dvals[diso]}
            if ev_key == "n_amount" and unit:
                fields["n_unit"] = unit
            evs.append({"fields": fields, "source": "column"})
        if evs:
            per_events[t] = evs
    if not per_events:
        return None
    # date + the mapped field come from the column (read-only); the paired field is blank.
    missing = "form" if ev_key == "n_amount" else "n_amount"
    return {"field": ev_key, "missing": missing, "locked": ["date", ev_key],
            "column": header, "per_treatment": per_events}

# Guardrail: never daily-fill a pathological span (a bad/outlier date) into millions of
# rows — beyond this, fall back to the observed dates only.
_MAX_FILL_DAYS = 20000


def _column_units(submission: UploadSubmission) -> dict:
    """canonical_field_id -> declared source unit from the mapping (None if unset)."""
    out: dict = {}
    for entry in (submission.mapping or {}).values():
        if isinstance(entry, dict):
            fid = entry.get("canonical_field")
            if fid:
                out[fid] = entry.get("unit")
    return out


def _event_row_values(submission: UploadSubmission, treatment: str) -> dict:
    """date_iso -> {row_field: canonical value} from dated timeline events for a treatment.

    N amounts are converted to canonical units here too (e.g. a rate the paper gave in
    g N m⁻² becomes kg N ha⁻¹), so a merged event row matches the rest of the series.
    """
    ev = submission.events if isinstance(submission.events, dict) else {}
    scopes = ev.get("scopes", {})
    glob = ev.get("global", {})
    per = (ev.get("per_treatment", {}) or {}).get(treatment, {})
    provided = set(_column_provided_event_sections(submission))
    out: dict = {}
    for section, rowmap in _EVENT_TO_ROW.items():
        if section in provided:
            continue  # the mapped column supplies this section's values
        mode = scopes.get(section, "global")
        lst = per.get(section, []) if mode == "per_treatment" else glob.get(section, [])
        for e in lst or []:
            f = (e or {}).get("fields", {}) or {}
            diso = _iso_date(f.get("date"))
            if not diso:
                continue
            row = out.setdefault(diso, {})
            for row_field, ev_key in rowmap.items():
                if ev_key is None:
                    row.setdefault(row_field, 1)
                    continue
                val = f.get(ev_key)
                if val in (None, ""):
                    continue
                if row_field == "nitrogenapplied":
                    num = _to_float(val)
                    if num is not None:
                        val = units.convert("nitrogenapplied", num, f.get("n_unit"))
                row.setdefault(row_field, val)
    return out


def _normalized_treatments(submission: UploadSubmission) -> dict:
    """Canonical, daily-continuous per-treatment series — the shared pre-submission shape.

    Per treatment: convert each mapped column to its canonical unit, key measurements by
    date, overlay paper-derived events onto their date's row, then fill a row for *every*
    calendar day between the treatment's first and last date. Returns the treatment list,
    the ordered column set, the chart series fields, and per-treatment ``{series, rows}``.
    Both Review and (later) Submit build on this.
    """
    import datetime as _dt

    sheet = _primary_sheet(submission)
    headers = sheet["headers"] if sheet else []
    rows = sheet.get("rows", []) if sheet else []
    colmap = _mapped_field_columns(submission)
    col_units = _column_units(submission)

    trt_col = _treatment_column(submission)
    trt_idx = headers.index(trt_col) if trt_col in headers else None
    date_idx = colmap.get("date")
    date_fmt = _submission_date_format(submission)
    treatments = _detected_treatments(submission, trt_col) or ["All data"]

    # 'replication' is the identifier we average over: Treatment/RawMeasurementTreatment
    # hold treatment-*averaged* data, so it isn't a measurement column itself.
    meas_fields = [
        f.id for f in canonical.CANONICAL_FIELDS
        if f.id in colmap and f.id not in ("treatment", "date", "replication")
    ]
    # Collapse to one treatment-averaged value per calendar day, in two levels:
    #   (a) sub-daily → daily *per replicate* (e.g. 15-min eddy-covariance data → a daily
    #       mean for each rep), then
    #   (b) replicate daily values → the treatment daily mean, with the N2O flux standard
    #       error taken across the replicate daily means (between-rep variance, not within-day).
    # A missing replication column ⇒ a single pseudo-replicate, so high-resolution data still
    # collapses to a daily mean (just with no SE — SE needs ≥2 reps).
    rep_idx = colmap.get("replication")
    rep_mapped = rep_idx is not None
    numeric_fields = {fid for fid in meas_fields
                      if canonical.get(fid) and canonical.get(fid).dtype in ("float", "int")}

    # Phase 1 — accumulate each source row as (timestamp, {field: value}) keyed by
    # (treatment, date, replicate). The timestamp lets a rep's sub-daily points be
    # time-weighted; date-only data just carries midnight.
    bad_dates: list[str] = []
    blank_treatment_rows = 0
    accum: dict = {t: {} for t in treatments}   # accum[t][diso][rep_key] = [(dt, {fid: val})]
    for row in rows:
        tname = (str(row[trt_idx]).strip()
                 if trt_idx is not None and trt_idx < len(row) else treatments[0])
        if tname not in accum:
            # With a treatment column mapped, an unmatched name means a blank/whitespace
            # cell (every real value is in `detected`) — the row can't be attributed, so
            # it's dropped; count it so Review/Submit can flag the loss.
            if trt_idx is not None:
                blank_treatment_rows += 1
            continue
        raw_date = row[date_idx] if date_idx is not None and date_idx < len(row) else None
        dt = _parse_datetime(raw_date, date_fmt)
        if dt is None:
            if raw_date not in (None, "") and len(bad_dates) < 10:
                sv = str(raw_date).strip()
                if sv not in bad_dates:
                    bad_dates.append(sv)
            continue  # a daily series needs a real date to place the row on
        diso = dt.date().isoformat()
        rep_key = (str(row[rep_idx]).strip() if rep_mapped and rep_idx < len(row) else "")
        fieldvals: dict = {}
        for fid in meas_fields:
            ci = colmap[fid]
            raw = row[ci] if ci < len(row) else None
            if raw in (None, ""):
                continue
            if fid in numeric_fields:
                num = _to_float(raw)
                if num is None:
                    continue
                fieldvals[fid] = units.convert(fid, num, col_units.get(fid))
            else:
                fieldvals[fid] = raw
        accum[tname].setdefault(diso, {}).setdefault(rep_key, []).append((dt, fieldvals))

    # Phase 2 — the two-level collapse (a) then (b).
    import statistics as _stats
    daily_averaged = 0   # treatment-dates that had >1 sub-daily point per replicate
    time_weighted = False  # at least one daily mean used timestamps (not a bare mean)
    per_dates: dict = {t: {} for t in treatments}
    for t in treatments:
        for diso, reps in accum[t].items():
            # (a) each replicate's sub-daily points → a daily value (time-weighted when the
            # points carry distinct timestamps, else a plain mean).
            rep_daily: dict = {}
            multi_point = False
            for rep_key, entries in reps.items():
                if len(entries) > 1:
                    multi_point = True
                dd: dict = {}
                fids_here = set().union(*(fv.keys() for _, fv in entries)) if entries else set()
                for fid in fids_here:
                    if fid in numeric_fields:
                        pairs = [(ts, fv[fid]) for ts, fv in entries if fid in fv]
                        dd[fid] = _time_weighted_mean(pairs)
                        if len(pairs) > 1 and (max(p[0] for p in pairs)
                                               - min(p[0] for p in pairs)).total_seconds() > 0:
                            time_weighted = True
                    else:
                        dd[fid] = next(fv[fid] for _, fv in entries if fid in fv)
                rep_daily[rep_key] = dd
            if multi_point:
                daily_averaged += 1
            # (b) replicate daily values → treatment daily mean (+ flux SE across reps).
            bucket = per_dates[t].setdefault(diso, {})
            fids = set().union(*(dd.keys() for dd in rep_daily.values())) if rep_daily else set()
            for fid in fids:
                if fid in numeric_fields:
                    vals = [dd[fid] for dd in rep_daily.values() if fid in dd]
                    bucket[fid] = sum(vals) / len(vals)
                else:
                    bucket[fid] = next(dd[fid] for dd in rep_daily.values() if fid in dd)
            if rep_mapped:
                flux = [dd["n2oflux"] for dd in rep_daily.values() if "n2oflux" in dd]
                if len(flux) >= 2:  # computed SE is authoritative when reps are present
                    bucket["fluxstandarderror"] = _stats.stdev(flux) / (len(flux) ** 0.5)
    reps_averaged = rep_mapped and any(
        len(reps) > 1 for dd in accum.values() for reps in dd.values())

    # Overlay events (never overwriting a real measurement); note which event columns appear.
    event_cols: set = set()
    for t in treatments:
        for diso, vals in _event_row_values(submission, t).items():
            bucket = per_dates[t].setdefault(diso, {})
            for k, v in vals.items():
                bucket.setdefault(k, v)
                event_cols.add(k)

    # DOY is always derived from the date; NitrogenApplied is dense (0 on non-application
    # days); a computed flux SE appears whenever replicates gave us one.
    has_se = any("fluxstandarderror" in b for dd in per_dates.values() for b in dd.values())
    present = set(meas_fields) | event_cols | {"doy"}
    if has_se:
        present.add("fluxstandarderror")
    ordered = [f.id for f in canonical.CANONICAL_FIELDS if f.id in present and f.id != "treatment"]
    columns = [{"id": "date", "label": canonical.get("date").label, "unit": None}] + [
        {"id": fid, "label": canonical.get(fid).label, "unit": canonical.get(fid).unit}
        for fid in ordered
    ]
    series_fields = [fid for fid in _REVIEW_SERIES_FIELDS if fid in present]
    zero_fill_napp = "nitrogenapplied" in present

    per_treatment: dict = {}
    for t in treatments:
        dmap = per_dates[t]
        series = {fid: [] for fid in series_fields}
        table_rows: list = []
        records: list = []
        if dmap:
            days = sorted(_dt.date.fromisoformat(k) for k in dmap)
            span = (days[-1] - days[0]).days
            # Daily fill unless the span is pathological, in which case use seen dates only.
            seq = ([days[0] + _dt.timedelta(days=i) for i in range(span + 1)]
                   if 0 <= span <= _MAX_FILL_DAYS else days)
            for day in seq:
                vals = dict(dmap.get(day.isoformat(), {}))
                vals["doy"] = day.timetuple().tm_yday           # derived for every day
                if zero_fill_napp:
                    vals.setdefault("nitrogenapplied", 0)       # no application ⇒ 0, not blank
                table_rows.append([day.isoformat()] + [_fmt_cell(vals.get(fid)) for fid in ordered])
                # Typed record (canonical values, not display strings) for promotion.
                rec = {"date": day.isoformat()}
                rec.update({fid: vals.get(fid) for fid in ordered})
                records.append(rec)
                ms = _to_millis(day.isoformat())
                for fid in series_fields:
                    v = vals.get(fid)
                    # Only plot real fertiliser applications, not the 0-filled days.
                    if fid == "nitrogenapplied" and (v is None or v == 0):
                        continue
                    fv = _to_float(v)
                    if ms is not None and fv is not None:
                        series[fid].append([ms, fv])
        per_treatment[t] = {"series": series, "rows": table_rows, "records": records}

    return {
        "treatments": treatments,
        "columns": columns,
        "series_fields": series_fields,
        "perTreatment": per_treatment,
        "date_format": date_fmt,
        "bad_dates": bad_dates,
        "blank_treatment_rows": blank_treatment_rows,
        "reps_averaged": reps_averaged,
        "daily_averaged": daily_averaged,
        "time_weighted": time_weighted,
    }


def _date_issue(submission: UploadSubmission) -> dict | None:
    """A user-facing complaint if some date values don't fit the declared format, else
    None. Used by the submit-readiness gate and the review banner."""
    norm = _normalized_treatments(submission)
    bad = norm.get("bad_dates") or []
    if not bad:
        return None
    label = next((f["label"] for f in DATE_FORMATS if f["id"] == norm.get("date_format")),
                 "auto-detect")
    return {"format": norm.get("date_format"), "format_label": label, "samples": bad}


def _orphaned_treatment_keys(submission: UploadSubmission) -> list[str]:
    """Annotation keys (names/descriptions/per-treatment soil-mgmt/events) that no longer
    match any detected treatment — stranded, e.g. after the treatment column was remapped.
    Their data would be silently dropped at promotion, so Review/Submit surfaces them."""
    detected = set(_detected_treatments(submission, _treatment_column(submission)))
    td = submission.treatment_details or {}
    ev = submission.events if isinstance(submission.events, dict) else {}
    keys: set = set()
    keys |= set((td.get("per_treatment") or {}).keys())
    keys |= set((td.get("names") or {}).keys())
    keys |= set((td.get("descriptions") or {}).keys())
    keys |= set((ev.get("per_treatment") or {}).keys())
    return sorted(k for k in keys if k not in detected)


def _coords_ok(lat, lon) -> bool:
    """Both present and within valid geographic range."""
    la, lo = _to_float(lat), _to_float(lon)
    return (la is not None and lo is not None
            and -90 <= la <= 90 and -180 <= lo <= 180)


def _columns_missing_units(submission: UploadSubmission) -> list[str]:
    """Mapped columns whose canonical field carries units but has no unit chosen — their
    values would be assumed canonical (a magnitude error). Backstop for the mapping-page
    requirement, so a crafted save can't slip an unconverted column through."""
    out: list[str] = []
    for header, entry in (submission.mapping or {}).items():
        if not isinstance(entry, dict):
            continue
        fid = entry.get("canonical_field")
        if fid and units.options_for(fid) and not entry.get("unit"):
            out.append(header)
    return out


def _build_review(submission: UploadSubmission) -> dict:
    """Assemble the read-only review payload from the normalized (canonical-unit,
    daily-continuous) series, adding event markers, per-treatment details, and the
    global site panel."""
    norm = _normalized_treatments(submission)
    descriptions = (submission.treatment_details or {}).get("descriptions", {}) or {}

    per_treatment: dict = {}
    for t in norm["treatments"]:
        nt = norm["perTreatment"][t]
        per_treatment[t] = {
            "series": nt["series"],
            "rows": nt["rows"],
            "events": _review_events(submission, t),
            "details": {
                "description": descriptions.get(t),
                "groups": _per_treatment_groups(submission, t),
            },
        }

    field_meta = {
        fid: {"label": canonical.get(fid).label, "unit": canonical.get(fid).unit}
        for fid in norm["series_fields"]
    }
    # Study-wide panel: Publication + Site (always one-per-study), plus any Treatment
    # group set to global scope (one value across all treatments) so it isn't repeated
    # under every treatment.
    study_wide = _global_info(submission) + _global_treatment_groups(submission)
    bad = norm.get("bad_dates") or []
    date_issue = None
    if bad:
        label = next((f["label"] for f in DATE_FORMATS if f["id"] == norm.get("date_format")),
                     "auto-detect")
        date_issue = {"format_label": label, "samples": bad}
    return {
        "treatments": norm["treatments"],
        # identifier -> optional readable name (shown in the switcher; identifier stays key).
        "names": (submission.treatment_details or {}).get("names", {}) or {},
        "columns": norm["columns"],
        "perTreatment": per_treatment,
        "global": study_wide,
        "fieldMeta": field_meta,
        "dateIssue": date_issue,
        "blankTreatmentRows": norm.get("blank_treatment_rows", 0),
        "orphanKeys": _orphaned_treatment_keys(submission),
        "repsAveraged": norm.get("reps_averaged", False),
        "dailyAveraged": norm.get("daily_averaged", 0),
        "timeWeighted": norm.get("time_weighted", False),
    }


def review(request, token):
    """Render the read-only Review stage: per-treatment tabular + stacked graphs, with
    the global site/study details in a side panel."""
    submission = _owned_submission(request, token)
    if _primary_sheet(submission) is None:
        return redirect("submit-data")

    data = _build_review(submission)
    return render(
        request,
        "workbench_review.html",
        {
            "submission": submission,
            "public_token": submission.public_token,
            "review_data": data,
            **_wizard_context(current_step=6, token=submission.public_token),
        },
    )


# --- Stage 7: submit (finalize the draft for curator review) -----------------

def _mapped_field_ids(submission: UploadSubmission) -> set:
    return {
        e["canonical_field"]
        for e in (submission.mapping or {}).values()
        if isinstance(e, dict) and e.get("canonical_field")
    }


def _resolved_global_value(submission: UploadSubmission, field_id: str):
    """Merged (saved answer > document-extracted) value for a global site field, or None.
    The single source of truth for both the readiness gate and promotion."""
    vals = _initial_site_values(submission, site_fields.global_fields())
    e = vals.get(field_id)
    if isinstance(e, dict) and e.get("value") not in (None, ""):
        return e["value"]
    return None


def _resolved_treatment_value(submission: UploadSubmission, treatment: str, field_id: str):
    """Value of a treatment-level field for one treatment, honouring its group's
    global/per-treatment scope (Soil & Management stage), or None."""
    td = submission.treatment_details or {}
    f = site_fields.get(field_id)
    mode = (td.get("scopes", {}) or {}).get(f.group, "global") if f else "global"
    src = ((td.get("per_treatment", {}) or {}).get(treatment, {})
           if mode == "per_treatment" else (td.get("global", {}) or {}))
    e = src.get(field_id)
    if isinstance(e, dict) and e.get("value") not in (None, ""):
        return e["value"]
    # Fall back to the document-extracted value only for GLOBAL-scope fields. The
    # manuscript extractor yields one value per field, so applying it to a *per-treatment*
    # field would silently give every treatment the same value (e.g. the paper's single
    # crop stamped on a fallow control) — a contamination we won't allow. A per-treatment
    # field must be filled explicitly per treatment.
    if mode == "global":
        m = (submission.manuscript_facts or {}).get("metadata", {}).get(field_id)
        if isinstance(m, dict) and m.get("value") not in (None, ""):
            return m["value"]
    return None


def _submit_readiness(submission: UploadSubmission) -> list[dict]:
    """Gating checks that must all pass before a draft can be submitted. These mirror
    the not-null columns promotion depends on (Site coordinates, Treatment.PrimaryCrop)."""
    have = _mapped_field_ids(submission)
    required = [f for f in canonical.CANONICAL_FIELDS if f.required]
    missing = [f.label for f in required if f.id not in have]
    treatments = _timeline_treatments(submission)
    coords_ok = _coords_ok(
        _resolved_global_value(submission, "site_latitude"),
        _resolved_global_value(submission, "site_longitude"),
    )
    crop_ok = bool(treatments) and all(
        _resolved_treatment_value(submission, t, "primary_crop") for t in treatments
    )
    date_issue = _date_issue(submission)
    norm = _normalized_treatments(submission)
    blank_rows = norm.get("blank_treatment_rows", 0)
    orphans = _orphaned_treatment_keys(submission)
    return [
        {"label": "Data file uploaded and parsed",
         "ok": bool((submission.parsed_content or {}).get("sheets"))},
        {"label": "Required columns mapped (date, N₂O flux, treatment)",
         "ok": not missing,
         "detail": ("Still needed: " + ", ".join(missing)) if missing else None},
        {"label": "Every mapped measurement column has a unit",
         "ok": not _columns_missing_units(submission),
         "detail": ("No unit chosen for: " + ", ".join(_columns_missing_units(submission))
                    + ". Set it on the Map step.") if _columns_missing_units(submission) else None},
        {"label": "All dates match the selected date format",
         "ok": date_issue is None,
         "detail": (f"These values don't fit {date_issue['format_label']}: "
                    + ", ".join(date_issue["samples"])
                    + ". Fix the date format on the Map step or correct the data.")
                   if date_issue else None},
        {"label": "Every row has a treatment", "ok": not blank_rows,
         "detail": (f"{blank_rows} row(s) have a blank treatment cell and would be dropped. "
                    "Fill the treatment column or remove those rows.") if blank_rows else None},
        {"label": "Treatment details all match the data", "ok": not orphans,
         "detail": ("Details entered for treatments no longer in the data (likely after "
                    "re-mapping): " + ", ".join(orphans)
                    + ". Re-open the Soil & Management step to reconcile.") if orphans else None},
        {"label": "At least one treatment identified", "ok": bool(treatments)},
        {"label": "Site coordinates valid (lat −90..90, lon −180..180)", "ok": coords_ok},
        {"label": "Primary crop set for every treatment", "ok": crop_ok},
    ]


def _submit_summary(submission: UploadSubmission) -> dict:
    """A compact tally of what will be submitted, from the normalized series."""
    norm = _normalized_treatments(submission)
    rows = sum(len(v["rows"]) for v in norm["perTreatment"].values())
    bounds: list[str] = []
    for v in norm["perTreatment"].values():
        if v["rows"]:
            bounds += [v["rows"][0][0], v["rows"][-1][0]]  # ISO dates sort lexically
    ev = submission.events if isinstance(submission.events, dict) else {}
    events = sum(len(x) for x in (ev.get("global") or {}).values()) + sum(
        len(x) for sec in (ev.get("per_treatment") or {}).values() for x in sec.values()
    )
    filled = sum(1 for g in _global_info(submission) for it in g["items"] if not it.get("empty"))
    return {
        "treatments": len(norm["treatments"]),
        "columns": max(len(norm["columns"]) - 1, 0),  # minus the date key column
        "rows": rows,
        "date_from": min(bounds) if bounds else None,
        "date_to": max(bounds) if bounds else None,
        "events": events,
        "global_filled": filled,
    }


def submit(request, token):
    """Render the final Submit stage: readiness, a submission summary, and (for a live
    draft) the contributor form. Once submitted, show the confirmation state instead."""
    submission = _owned_submission(request, token)
    if _primary_sheet(submission) is None:
        return redirect("submit-data")

    ctx = {
        "submission": submission,
        "public_token": submission.public_token,
        "submitted": submission.status != "draft",
        **_wizard_context(current_step=7, token=submission.public_token),
    }
    if not ctx["submitted"]:
        from .services import promote
        checks = _submit_readiness(submission)
        owner = submission.owner
        ctx.update({
            "checks": checks,
            "ready": all(c["ok"] for c in checks),
            "summary": _submit_summary(submission),
            "contributor_name": submission.contributor_name
            or (owner.get_full_name() if owner else ""),
            # Bound to the verified account and shown read-only (not editable/spoofable).
            "contributor_email": _verified_email(owner),
            # Soft, non-blocking: alert the submitter if this paper's DOI is already in
            # the database — a second dataset for the same paper is allowed to proceed.
            "doi_duplicates": promote.find_duplicate_publications(
                _resolved_global_value(submission, "pub_doi")
            ),
        })
    return render(request, "workbench_submit.html", ctx)


@require_POST
def finalize_submit(request, token):
    """Finalize a draft: validate readiness + contributor identity, then move it to
    ``submitted`` for curator review. Does not touch the live database — promotion into
    RawMeasurementTreatment is a separate, curator-driven step."""
    from django.urls import reverse

    submission = _owned_submission(request, token)
    if submission.status != "draft":
        return JsonResponse({"error": "This submission has already been sent."}, status=400)

    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    checks = _submit_readiness(submission)
    if not all(c["ok"] for c in checks):
        return JsonResponse(
            {"error": "Some required steps are still incomplete.", "checks": checks}, status=400
        )

    name = str(payload.get("name", "")).strip()
    # The email is bound to the verified account, never taken from the request body —
    # so it can't be spoofed for the trust signal or to redirect our notifications.
    email = _verified_email(submission.owner)
    fields: dict = {}
    if not name:
        fields["name"] = "Please enter your name."
    if not email:
        # Login requires a verified email, so this is effectively unreachable — but
        # fail loudly rather than record an unattributed submission.
        return JsonResponse(
            {"error": "Your account has no verified email address. Please verify your "
                      "email before submitting."},
            status=400,
        )
    if fields:
        return JsonResponse({"error": "Please fix the highlighted fields.", "fields": fields}, status=400)

    submission.contributor_name = name
    submission.contributor_email = email
    message = str(payload.get("message", "")).strip()
    if message:
        notes = submission.review_notes if isinstance(submission.review_notes, list) else []
        notes.append({"author": "contributor", "note": message, "at": timezone.now().isoformat()})
        submission.review_notes = notes
    submission.status = "submitted"
    submission.submitted_at = timezone.now()
    submission.save(update_fields=[
        "contributor_name", "contributor_email", "review_notes",
        "status", "submitted_at", "updated_at",
    ])

    # A finalized submission is no longer the session's editable draft; the next visit
    # to /submit-data/ should start a fresh one.
    if request.session.get("workbench_token") == str(submission.public_token):
        request.session.pop("workbench_token", None)

    return JsonResponse(
        {"status": "submitted", "redirect": reverse("workbench-submit", args=[submission.public_token])}
    )


# --- Curation (staff): review submitted contributions & promote --------------

# Non-draft statuses a curator works through, newest first.
_CURATION_STATUSES = ("submitted", "in_review", "changes_requested", "approved", "promoted", "rejected")


@staff_member_required
def curation_list(request):
    """Staff queue of contributed submissions awaiting (or past) review."""
    from .services import promote

    subs = (UploadSubmission.objects
            .exclude(status="draft")
            .order_by("-submitted_at", "-updated_at"))
    rows = []
    for s in subs:
        doi = _resolved_global_value(s, "pub_doi")
        rows.append({
            "token": s.public_token,
            "title": _resolved_global_value(s, "pub_title") or "(untitled)",
            "contributor": s.contributor_name or "—",
            "status": s.status,
            "status_label": s.get_status_display(),
            "submitted_at": s.submitted_at,
            "treatments": len(_timeline_treatments(s)),
            "dup_doi": bool(promote.find_duplicate_publications(doi)),
            "trust": _email_trust(s.contributor_email),
        })
    return render(request, "workbench_curation_list.html", {"rows": rows})


# Consumer/free email providers — an address here carries less institutional weight
# than an academic/organisational one when a curator judges a dataset's provenance.
_PERSONAL_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com", "yandex.com", "zoho.com", "qq.com", "163.com", "126.com",
})


def _email_trust(email) -> dict | None:
    """Classify an email's domain — academic / personal / other — as an at-a-glance
    validity signal for the curator. Institutional addresses lend a dataset weight."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return None
    if domain in _PERSONAL_EMAIL_DOMAINS:
        kind, label = "personal", "Personal email"
    elif domain.endswith((".edu", ".ac")) or ".edu." in domain or ".ac." in domain:
        kind, label = "academic", "Academic / institutional"
    else:
        kind, label = "other", "Organisation / other"
    return {"domain": domain, "kind": kind, "label": label}


@staff_member_required
def curation_detail(request, token):
    """Full curation view for one submission: the embedded graph/table review, a promotion
    preview (exactly what would be written, plus blocking/warning issues and DOI
    collisions), contributor trust signals, and the review-note history."""
    from .services import promote

    submission = _owned_submission(request, token)
    owner = submission.owner
    account_email = (getattr(owner, "email", "") or "") if owner else ""
    account_verified = False
    if owner is not None:
        try:
            from allauth.account.models import EmailAddress
            account_verified = EmailAddress.objects.filter(user=owner, verified=True).exists()
        except Exception:  # noqa: BLE001 — a trust hint must never break the page
            account_verified = False

    return render(
        request,
        "workbench_curation_detail.html",
        {
            "submission": submission,
            "preview": promote.dry_run(submission),
            "status": submission.status,
            "status_label": submission.get_status_display(),
            # The full graph/table review is embedded inline (shared _review_panel).
            "review_data": _build_review(submission),
            "notes": submission.review_notes if isinstance(submission.review_notes, list) else [],
            # Contributor trust signals.
            "contributor_trust": _email_trust(submission.contributor_email),
            "account_email": account_email,
            "account_trust": _email_trust(account_email),
            "account_verified": account_verified,
            "account_email_differs": bool(
                account_email and account_email.lower() != (submission.contributor_email or "").lower()
            ),
        },
    )


def _email_contributor(submission: UploadSubmission, subject: str, body: str) -> None:
    """Best-effort notification to the contributor. A mail failure must never break a
    curator's action, so it's swallowed (and logged)."""
    from django.core.mail import send_mail

    to = (submission.contributor_email or "").strip()
    if not to:
        return
    try:
        send_mail(subject, body, None, [to], fail_silently=True)
    except Exception:  # noqa: BLE001 — notification is a nicety, never a hard dependency
        logger.exception("failed to email contributor for submission %s", submission.pk)


def _decision_email(submission, kind, note, resume_url=None):
    """(subject, body) for a curator decision email. Plain text; kept simple."""
    name = submission.contributor_name or "there"
    title = _resolved_global_value(submission, "pub_title") or "your dataset"
    if kind == "changes_requested":
        subject = "Changes requested on your GRAND Explorer submission"
        body = (
            f"Hello {name},\n\n"
            f"A curator has reviewed your submission “{title}” and asked for some "
            f"changes before it can be published.\n\n"
            f"Curator note:\n{note or '(no note provided)'}\n\n"
            f"Resume editing here, then submit again for review:\n{resume_url}\n\n"
            f"— The GRAND Explorer team"
        )
    elif kind == "rejected":
        subject = "Update on your GRAND Explorer submission"
        note_block = f"Curator note:\n{note}\n\n" if note else ""
        body = (
            f"Hello {name},\n\n"
            f"After review, your submission “{title}” was not accepted for the "
            f"GRAND Explorer database.\n\n"
            f"{note_block}"
            f"If you believe this was in error, just reply to this email.\n\n"
            f"— The GRAND Explorer team"
        )
    else:  # promoted
        subject = "Your GRAND Explorer submission has been published"
        body = (
            f"Hello {name},\n\n"
            f"Good news — your submission “{title}” has been reviewed and "
            f"published in the GRAND Explorer database. Thank you for contributing.\n\n"
            f"— The GRAND Explorer team"
        )
    return subject, body


@staff_member_required
@require_POST
def curation_action(request, token):
    """Apply a curator decision (request changes / reject / promote) and notify the
    contributor by email."""
    from .services import promote

    submission = _owned_submission(request, token)
    action = request.POST.get("action")
    note = (request.POST.get("note") or "").strip()

    # Requesting changes without saying what to change is useless to the contributor.
    if action == "request_changes" and not note:
        messages.error(request, "Please add a note describing the changes needed — "
                                "it's emailed to the contributor.")
        return redirect("workbench-curation-detail", token=token)

    if note:
        notes = submission.review_notes if isinstance(submission.review_notes, list) else []
        notes.append({"author": "curator", "note": note, "at": timezone.now().isoformat()})
        submission.review_notes = notes
        submission.save(update_fields=["review_notes", "updated_at"])

    simple = {"approve": "approved", "request_changes": "changes_requested", "reject": "rejected"}
    if action in simple:
        submission.status = simple[action]
        submission.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Submission marked “{submission.get_status_display()}”.")
        if action == "request_changes":
            resume_url = request.build_absolute_uri(
                reverse("workbench-resume", args=[submission.public_token]))
            _email_contributor(submission, *_decision_email(
                submission, "changes_requested", note, resume_url))
        elif action == "reject":
            _email_contributor(submission, *_decision_email(submission, "rejected", note))
    elif action == "promote":
        try:
            result = promote.promote(submission)
            messages.success(
                request,
                f"Promoted to the live database — {result['raw_count']} measurement rows "
                f"across {len(result['treatmentids'])} treatment(s).",
            )
            _email_contributor(submission, *_decision_email(submission, "promoted", note))
        except promote.PromotionError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Unknown action.")

    return redirect("workbench-curation-detail", token=token)

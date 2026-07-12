"""Assisted-upload workbench models.

Two things live here:

* ``UploadSubmission`` — the durable draft "envelope" a contributor builds up as
  they walk the upload wizard (roadmap §3.1). Interactive editing state is kept as
  JSON so there is no schema churn per keystroke; typed staging rows are
  materialised later, at "submit for review".
* ``ManuscriptSource`` — an optional source document (PDF/DOCX/pasted text) attached
  to a submission, whose plain text is extracted up front and stored for the
  manuscript-extraction feature (manuscript-extraction-implementation-plan §2).

These are ``managed=True`` (the default), so they become ordinary migrations. Run
``makemigrations``/``migrate`` inside the app container.
"""
import uuid

from django.conf import settings
from django.db import models


class UploadSubmission(models.Model):
    """The durable draft envelope for one contributed study.

    Only the fields the manuscript-extraction plan actually touches are exercised
    today; the remaining JSON fields are the roadmap's wizard state (parsed
    content, column mapping, Plane B/C answers, etc.) and default to empty so the
    later wizard stages can fill them in without another migration.
    """

    STATUS = [
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("in_review", "In review"),
        ("changes_requested", "Changes requested"),
        ("approved", "Approved"),
        ("promoted", "Promoted"),
        ("rejected", "Rejected"),
    ]

    # Stable, unguessable handle used for resumable/shareable wizard links.
    public_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="upload_submissions",
    )
    contributor_email = models.EmailField(blank=True)
    contributor_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="draft")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    # --- interactive wizard state (JSON while editing; roadmap §3.1) ---
    parsed_content = models.JSONField(default=dict, blank=True)
    structure = models.JSONField(default=dict, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    # Global (one-per-study) Plane B answers: Publication + Site & climate.
    metadata_values = models.JSONField(default=dict, blank=True)
    # Soil & management answers, which may be global or per-treatment. Shape:
    #   {treatments: [...names], descriptions: {<name>: <text>},
    #    treatment_column: <header|null>,
    #    scopes: {<group>: "global"|"per_treatment"},
    #    global: {<field_id>: {value, source, ...}},
    #    per_treatment: {<name>: {<field_id>: {value, source, ...}}}}
    treatment_details = models.JSONField(default=dict, blank=True)
    events = models.JSONField(default=list, blank=True)
    augmentation = models.JSONField(default=dict, blank=True)
    review_notes = models.JSONField(default=list, blank=True)
    qc = models.JSONField(default=dict, blank=True)

    # --- promotion (idempotency record) ---
    # Set once a curator promotes the submission into the live GRAND tables. Holds the
    # created primary keys ({pubid, siteid, experimentid, treatmentids, raw_count}); its
    # presence is the structural guard that a submission is never promoted twice.
    promotion = models.JSONField(default=dict, blank=True)

    # --- manuscript extraction (manuscript plan §2.2) ---
    # Opaque JSON produced by the extraction module (§5). The plumbing does not
    # interpret its contents — it renders them. Shape is the §5 contract.
    manuscript_facts = models.JSONField(default=dict, blank=True)
    manuscript_extracted_at = models.DateTimeField(null=True, blank=True)
    # sha256 of the document that produced the *current* successful extraction. Used to
    # skip a redundant model call when Extract is re-run on an unchanged, already-analysed
    # paper (an accidental-cost guard). Empty until a run returns status "ok".
    manuscript_source_sha256 = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"UploadSubmission {self.public_token}"


class ManuscriptSource(models.Model):
    """A source document attached to a submission, with its extracted plain text."""

    KIND = [("pdf", "PDF"), ("docx", "DOCX"), ("text", "Pasted text")]
    STATUS = [
        ("pending", "Pending"),
        ("extracted", "Extracted"),
        ("failed", "Failed"),
    ]

    submission = models.ForeignKey(
        UploadSubmission, on_delete=models.CASCADE, related_name="documents"
    )
    kind = models.CharField(max_length=8, choices=KIND)
    original_name = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.IntegerField(null=True, blank=True)
    # Original bytes: DB blob for MVP (Railway FS is ephemeral; no MEDIA/S3 set up).
    # Null for pasted text. Swap to a volume/S3 path later if re-download is wanted.
    file_bytes = models.BinaryField(null=True, blank=True)
    extracted_text = models.TextField(blank=True)
    page_count = models.IntegerField(null=True, blank=True)
    char_count = models.IntegerField(null=True, blank=True)
    extraction_status = models.CharField(max_length=10, choices=STATUS, default="pending")
    extraction_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} for {self.submission_id}: {self.original_name or '(pasted)'}"


class LlmExtractionCall(models.Model):
    """One record per *actual* manuscript-extraction model call (status ok/failed).

    Durable, queryable audit trail of contributor LLM usage — the basis for the
    per-account daily cap (``WORKBENCH_LLM_DAILY_CAP``) and future cost monitoring.
    Deduped no-ops and disabled/skipped runs are never logged, so a count of rows
    for a user on a given day equals the model calls that actually cost money.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="llm_extraction_calls",
    )
    submission = models.ForeignKey(
        UploadSubmission,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="llm_calls",
    )
    status = models.CharField(max_length=10)  # "ok" | "failed"
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self):
        return f"LLM call by {self.user_id} ({self.status}) at {self.created_at:%Y-%m-%d %H:%M}"


class UploadedDataFile(models.Model):
    """A tabular data file (CSV/XLSX) attached to a submission.

    The parsed, normalized content lives on ``UploadSubmission.parsed_content``
    (the source-agnostic contract in ``services/tabular.py``); this row just
    records the original file for provenance and re-parsing. Original bytes are a
    DB blob for the MVP (Railway FS is ephemeral) and may be null once we move to
    a volume/S3.
    """

    KIND = [("csv", "CSV"), ("xlsx", "Excel")]
    STATUS = [
        ("pending", "Pending"),
        ("parsed", "Parsed"),
        ("failed", "Failed"),
    ]

    submission = models.ForeignKey(
        UploadSubmission, on_delete=models.CASCADE, related_name="data_files"
    )
    kind = models.CharField(max_length=8, choices=KIND)
    original_name = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.IntegerField(null=True, blank=True)
    file_bytes = models.BinaryField(null=True, blank=True)
    sheet_count = models.IntegerField(null=True, blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    parse_status = models.CharField(max_length=10, choices=STATUS, default="pending")
    parse_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} for {self.submission_id}: {self.original_name}"

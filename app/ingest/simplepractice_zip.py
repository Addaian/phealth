"""
SimplePractice Data Export ingester — top-level orchestrator.

Walks a SimplePractice "Complete" Data Export and drives the per-document
pipeline:

    PDF text extraction  (pdf_parser)
      -> document classification + section splitting  (section_detector)
      -> scale extraction  (scale_extractor)
      -> entity tagging  (entity_tagger)
      -> ASAM evidence indexing  (asam_evidence_index)
      -> TJC coverage update  (tjc_coverage_matrix)
      -> section embeddings  (embeddings)
      -> FHIR mapping  (app.fhir.mappers)

Implemented in M6. See documents/phase_1_implementation_plan.md §M6 and
documents/phase_1.md §Step 6.

Export-format note (discovered during M3 export inspection):
  - The export is a directory tree, not a single ZIP. Layout observed:
      Contacts/<name> - <id>.vcf                         (demographics)
      Client records/Medical Records/<client>/*.pdf      (chart notes)
      Client records/Billing Documents/<client>/...      (ignore)
  - Demographics export as vCard (.vcf), NOT CSV as the playbook assumed —
    the contacts parser here must read vCard.
  - Chart-note PDFs are text-layer (pdfplumber extracts cleanly); no OCR path
    is required.
"""

# Orchestration logic is implemented in M6.

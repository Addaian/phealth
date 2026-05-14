"""
Document classifier and section splitter.

Regex-first:
  - Classifies a document as ``bps_intake | soap | dap | dsap`` by dispatching
    on the bracketed template title (``[BPS]`` / ``[SOAP]`` / ``[DAP]`` /
    ``[DSAP]``) that SimplePractice prints verbatim in the PDF text.
  - Splits the body into format-specific sections, recording a char-offset
    span for each section.

LLM fallback runs only on a regex miss, bounded by ``MAX_LLM_CALLS_PER_INGEST``
(PRD §7). Each result records its ``extraction_method`` (regex | llm).

Implemented in M6.
"""

# Classification and splitting logic is implemented in M6.

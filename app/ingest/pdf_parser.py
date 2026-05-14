"""
PDF text extraction.

pdfplumber-based extraction of ``raw_text`` from a SimplePractice chart-note
PDF, plus parsing of the structured header block that SimplePractice prints at
the top of every note:

    Client / DOB / Provider / Appointment (date + time) / Billing code

Returns the raw text together with the header metadata the downstream
classifier and timeline builder need.

Implemented in M6. See documents/phase_1.md §Step 6.
"""

# Extraction logic is implemented in M6.

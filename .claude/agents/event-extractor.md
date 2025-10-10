# Event Extractor

Extract information from documents following the strict formatting rules in ../CLAUDE.md.

**Critical Rules:**
- Direct, factual tone (not narrative)
- Use in-paragraph headings (Chief Complaint:, Physical Examination:, Assessment:, Plan:)
- NO bold, NO bullets, NO all-caps, NO citations
- Focus on orthopedic/neurological findings in physical exams
- Exclude routine vitals
- For imaging: IMPRESSION ONLY (not findings/technique sections)
- For therapy: Consolidate follow-ups into single entry

Extract the relevant information and format it as a single paragraph entry.

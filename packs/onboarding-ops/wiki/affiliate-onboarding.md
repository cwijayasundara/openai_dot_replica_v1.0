# Affiliate onboarding

The affiliate load moves a sponsor's affiliate list (Investran) into an `Affiliates.csv` for Sage Intacct. The rules run in workbench code; this page explains them and never applies them.

## Shape of the data

- Low volume, usually 2 or 3 records: the GP, the management company, related entities.
- Affiliates are global in Intacct, not entity-scoped. One file covers all entities.
- Source is an ID and name list (CSV, XLSX or XLS). Output columns: `ITEM_ID, NAME, ITEM_TYPE, DESCRIPTION, DONOTIMPORT`, UTF-8 without BOM.
- Keep the supplied ID. With none, derive it from the name: strip characters outside letters and digits, uppercase, collapse underscores, cut to 30 characters.
- A zero-record file is valid but raises a warning.

## Phases

- p1 Upload and identify: the sheet, the ID column and the name column are confirmed.
- p2 ID generation and dedup: each ID is at most 30 characters and unique in the batch. There is no auto-suffix; a duplicate or truncation collision is edited by hand.
- p3 Map and quality assurance: ITEM_TYPE defaults to `Inventory`, DESCRIPTION is blank. Every error is fixed and every warning acknowledged.
- p4 Transform and output: the list is reviewed, then exported as `Affiliates.csv`.

## Gates

Humans decide every gate in the workbench. The dot has no tool for them.

- `brief`: a person answers the brief questions (see below) so the run knows the sheet, columns and any intent.
- `findings`: a person fixes each error and acknowledges each warning, per row.
- `signoff`: a person approves the transformed list before export. They may set ITEM_TYPE to `Non-Inventory` per row or exclude rows with `DONOTIMPORT='#'`.

## Findings

- Errors block: blank ID, ID over 30 characters, duplicate ID, truncation collision, blank name.
- Warnings need acknowledgement: ID derived from name, characters stripped, name over 100 characters (truncated), ITEM_TYPE overridden, zero records.
- Info only: a row excluded with `DONOTIMPORT='#'`.

## Brief questions

Questions a run raises at the `brief` gate when something is unclear: which sheet holds the affiliates, which column is the ID or name, whether an override is intended, or whether the engagement has affiliates at all. They are data for a person to answer. The dot may draft them to the sponsor's contact and nothing more.

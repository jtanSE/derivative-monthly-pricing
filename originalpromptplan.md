You are a senior quantitative systems architect and institutional investment operations engineer designing an enterprise-grade month-end derivative pricing automation platform for an insurance company investment department.
 
The current workflow is heavily manual and involves receiving brokerage Excel files, reconciling derivative pricing and market values, calculating normalized pricing values, maintaining a canonical internal CSV/table, and eventually generating PAM messages for loading into the PFI investment database.
 
The system should be designed as a scalable, auditable, deterministic monthly batch-processing platform with institutional-grade reconciliation and validation capabilities.
 
An important fact is that the current operational workflow already exists in a manually maintained Excel/CSV workbook created by the Lead Operations Analyst. This workbook effectively acts as the current operational canonical dataset and contains important formulas, calculations, reconciliation logic, and final transformed values that are later used to build PAM messages. The automation system should be designed by reverse engineering and formalizing this existing workflow rather than inventing an entirely new operational model from scratch.
 
The workflow and business requirements are as follows:
 
1. Brokerage month-end statement files
 
* We receive multiple brokerage month-end Excel files containing derivative pricing and MTM/market value information.
* Each brokerage format differs from the others.
* However, each brokerage format is internally consistent month-to-month.
* The system must learn to parse each brokerage format separately and deterministically.
* This brokerage-specific parsing layer is one of the most important architectural requirements.
* I will provide several example brokerage Excel files representing real month-end statements.
* The architecture should support maintaining broker-specific parsers and mappings independently.
* Some brokerage files may contain double-legged option trades, which require special parsing and matching logic.
* Double-legged trades may appear differently in brokerage files and in the traders database, so the system must carefully handle matching by trade ID, leg ID if available, counterparty ID, underlying/index, expiration date, strike, shares/units, and other identifying fields.
* Brokerage trade confirmation PDFs also exist.
* I will provide several example trade confirmation PDFs.
* PDF parsing should be considered a future enhancement and not part of the initial MVP unless justified.
 
2. Traders database/export
 
* We have access to a traders database export through Excel rather than direct database access.
* I will provide example trader database export files.
* For the initial version, the traders database should be treated as the source of truth.
* The traders database contains the full trade population and high-level trade data.
* We need to maintain our own canonical live-trades CSV/table as the final internal dataset for this workflow.
* This canonical CSV/table should store all necessary trade, counterparty, pricing, MTM, notional, FX, reconciliation, and PAM-related fields needed for month-end processing.
* After validation and reconciliation, the final approved monthly data should live in this canonical CSV/table and be used later to generate the PAM message.
* A live trade means a trade whose expiration date has not passed.
* If a trade’s expiration date has passed, it is generally considered expired; however, if it has not settled yet, it may still appear in broker or trader records and may cause MTM differences between the traders database and brokerage files.
* New trades from the traders database should be added automatically to the canonical live-trades CSV/table.
* Existing live trades should be updated as needed.
* Expired and settled trades should be archived or removed from the active live-trades dataset.
* Initially, the system should focus only on call options, but the architecture should support future extension to put options and other derivative types.
 
3. Canonical operational workbook
 
* I will provide the Lead Operations Analyst’s manually maintained canonical Excel/CSV workbook.
* This workbook is extremely important because it represents the current operational process and final transformed dataset before PAM message creation.
* The workbook contains important formulas, calculations, reconciliation logic, MTM comparisons, and operational transformations.
* Some formulas embedded in workbook cells will need to be extracted, understood, and converted into deterministic programmatic calculation logic.
* The automation platform should treat this workbook as the operational target model for the MVP.
* The final automated canonical dataset should replicate the operational outputs of this workbook as closely as possible before PAM generation.
 
4. Counterparty ID and trade ID validation
 
* Some trades in the traders database or brokerage files may have missing, inconsistent, or differently formatted trade IDs or counterparty IDs.
* For the initial version, assume the traders database is correct unless validation shows otherwise.
* The system should still compare and validate trade IDs and counterparty IDs across the traders database, brokerage files, and canonical live-trades CSV/table.
* If counterparty ID or trade ID does not match, the system should flag an exception rather than silently overwrite values.
* The architecture should support future deterministic counterparty matching and enrichment.
* The plan should discuss matching strategies, matching confidence levels, exception handling, and manual review workflows.
 
5. Pricing and valuation workflow
 
* The pricing and valuation workflow includes:
 
  * Pulling index prices from PFI and/or Bloomberg.
  * Pulling FX and currency conversion data from PFI and/or Bloomberg.
  * Pulling MTM/market values from brokerage Excel files.
  * Pulling MTM/market values from the traders database.
  * Comparing broker MTM versus trader database MTM.
  * Determining which valuation “view” each MTM value represents:
 
    * broker valuation view
    * trader database/internal view
    * client-side valuation view
  * Explicitly storing valuation source/view metadata so the system knows where each valuation originated.
  * Calculating total notional using formulas such as shares × index price for most indices.
  * Supporting formula variations depending on trade ID, product type, underlying/index, currency, or deal-specific logic.
  * Calculating normalized per-unit pricing where needed, such as trader database MTM divided by shares/units.
  * Calculating total notional values in local currency and, where applicable, USD.
 
6. Market value and MTM reconciliation
 
* Market value means MTM.
* MTM is pulled from both brokerage Excel files and the traders database.
* The system should compare broker valuation versus trader database valuation.
* Differences may be caused by:
 
  * valuation view differences
  * expired-but-unsettled trades
  * timing differences
  * settlement status
  * double-legged trade representation
  * FX differences
  * data quality issues
* The system should flag differences outside approved tolerance thresholds.
* The reconciliation framework should be auditable and explainable.
 
7. Intermediate operational outputs
 
* We may initially use our own intermediate CSV/Excel outputs to:
 
  * Validate calculations.
  * Confirm reconciliation results.
  * Allow operational review with the Director of Operations.
  * Improve transparency during implementation.
  * Help compare system-generated values against the current manual process.
 
8. PAM messages and PFI integration
 
* The final output of the initial system should be the validated canonical live-trades CSV/table.
* PAM message generation should be treated as a later phase after the data model, calculations, matching, and reconciliation are stable.
* The future PAM message builder should consume the canonical live-trades CSV/table, not raw broker files directly.
* The eventual system will load data into the PFI investment database through PAM messages.
 
Design a complete system architecture and implementation plan including:
 
* High-level architecture.
* Canonical live-trades CSV/table schema.
* Reverse engineering strategy for the operational workbook.
* Monthly batch processing workflow.
* Landing zone/staging architecture.
* File ingestion and Excel parsing architecture.
* Broker-specific parser architecture.
* Deterministic normalization strategy.
* Double-legged trade parsing and matching strategy.
* Trade ID and counterparty ID matching strategy.
* Pricing and valuation engine design.
* Formula extraction and formula management strategy.
* Valuation source/view tracking.
* MTM comparison and reconciliation engine design.
* Business rules engine design.
* Exception handling workflows.
* Auditability and logging.
* Data lineage.
* Validation framework.
* Suggested database/file design.
* Suggested technology stack.
* MVP scope versus future enhancements.
* Operational risks and controls.
* Recommended implementation phases.
* Potential future AI-assisted workflows.
* Institutional concerns for financial systems and derivative operations.
 
Be extremely detailed and think like an enterprise investment systems architect working at a large insurance company with strict operational, valuation, reconciliation, and audit requirements.
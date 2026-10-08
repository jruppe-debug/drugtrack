<analysis>
**Session flow**
- The session continued from an earlier summary. I answered the source-of-truth question and built a candidate table: script `$SP/approved_candidates.py`, output `$SP/approved_candidates.csv`, 46 candidates out of 545 drugs.
- The user then gave the inclusion/exclusion criteria.
- I started implementing a US-only AML gate in `export_disease_tree.py`.
- I read several regions of that file. No edits have been made yet.

**Key details to preserve**
- The category assignment code (lines 1133–1146).
- The letter loop (1186–1206).
- The `record_dates` / `reference_claims` loop, which ends around line 1229, just before "# ---- fidelity".
- The `byc` / `approved_ds` / `elsewhere_ds` block (1562–1574).
- The approved branch (1598–1602).
- The regression checks at 1828–1834 and 1862, the tests at 1942–1949, and the counts at 1995.
- The constants at lines 45–52.
- The `disease.html` category colours and help text (lines 100–153).
- `central_anchor.py` line 128: `only_scope`.
- `aml_pathways.py` CPX regimen.
</analysis>

<summary>
1. Primary Request and Intent:

   **Earlier governing requests (done)**
   - AML clinician decision tree on http://localhost:4300/disease, following PMC11966364 figs 2–5 plus hemonc treatment lines.
   - Levels: population, subgroup, phase, regimen, then lead/co-basis trials.
   - FDA label section 14 is the source of truth for combination leads.
   - Midostaurin central trial is NCT00651261.
   - "AML approvals only". Standardize across all approved AML drugs.

   **Previous request (answered)**
   > "what is the soruce of truth used to group drugs into the approved category. need this to atelast be high fidelity before debugging other issues. expand it to as many as possible and I will input human cirtiera for inclusion or exclusin"

   **Latest user request, verbatim (now implementing)**
   > "asersenic trioxide is only apprved in APL, which is a rare diesease and should not be consdiered within this network. other regulators should not be factored in directly, only as reference populations in supplemental tables if also approved in the US. supportive care drugs should be consdiered as supplemental regimens, not direct treaments. spportive care or conditioning drugs should only be referenced as part of the tratment that the original drug is used in. these treatments (ie generic 7 + 3 regimens) shoudl be referenced only when in combination with my approval drug pathway. make these adjustments to specify downt he list to avooid false positives. I will eventually build out a chart for CLL, APL, etc but do not want them contambinating the AML table"

2. Key Technical Concepts:

   **Running things**
   - Working directory: `$HOME/Clinical Tracker/DrugTrack`. Not a git repo.
   - Python: `PYTHONIOENCODING=utf-8 "$HOME/Clinical Tracker/.venv/Scripts/python.exe"`.
   - SP = `$TEMP/claude/C--Users-jwrup-Clinical-Tracker/e9a10f28-7091-42b1-af51-3a05c62b52fa/scratchpad`.
   - Pipeline: `tools/match_pipeline.py run` calls `export_disease_tree.py` to build `data/tree.json`.
   - Run order:
     1. `tools/central_anchor.py`
     2. `tools/label_s14.py`
     3. `$SP/reattach.py` (calls `attach_pathways` and writes tree.json; keeps `tree.json.prev`)
   - `OPENFDA_API_KEY` is loaded via dotenv from `CT_DIR/.env`. It is not in the shell env.

   **Current category logic (`export_disease_tree.py:1133-1146`)**
   - APPROVED_AT_SCOPE: `ot_approved` or OT regulatory evidence with relation exact/descendant, from any jurisdiction.
   - APPROVED_BROADER_TERM: an ancestor with distance ≤ 2.
   - LABEL_ONLY: US label `treatment_mentions`.
   - INVESTIGATIONAL: everything else.

   **Letters**
   - FDA approval letters (`d["letters"]`, `d["scope_first_letter"]`) are fetched only for non-INVESTIGATIONAL drugs. This happens after categorisation, at lines 1186–1206.
   - So the new gate must run after the letter loop.

   **Drug attrs in tree.json**
   - `category`, `jurisdictions`, `ot_approved_at_scope`, `us_label_scope_mention`, `approved_as_partner_in` (from `d["partner_in"]`), `scope_named_by`, `therapeutic_role.tag`, `ema_indications[].names_scope`.
   - `outside_sources.drugcentral.curated_indications`, `scope_indication_first_letter`, `approval_evidence`.

3. Files and Code Sections:

   **`$SP/approved_candidates.py` (created) and `$SP/approved_candidates.csv`**
   - Joins tree.json drug attrs with `data/central_anchor.json` and `data/label_s14.json`.
   - Columns: drug, chembl, category, ot_approved_at_scope, ot_reg_exact, ot_reg_ancestor, us_label_names_aml, ema_names_aml, drugcentral_aml, fda_letter_names_aml, letter_term, anchor_status, s14_events, partner_in, role, scope_named_by, jurisdictions.
   - Result: 46 candidates.
     - FDA letter dates: AZA 2020-09-01, CYT/DAU 2017-08-03, ENA 2017-08-01, GO 2017-09-01, GILT 2018-11-28, GLAS 2018-11-21, IVO 2018-07-20, MIDO 2017-04-28, OLU 2022-12-01, QUIZ 2023-07-20, CEDAZ 2026-05-13, REVU 2025-10-24, VEN 2018-11-21, ZIFTO 2025-11-13.
     - No US label AML mention: ARSENIC TRIOXIDE, TRETINOIN, HISTAMINE, MELPHALAN, TAGRAXOFUSP, DECITABINE.
     - US label names AML but no letter: DOXORUBICIN, IDARUBICIN, MITOXANTRONE, THIOGUANINE, SARGRAMOSTIM.

   **`tools/export_disease_tree.py` (read only, NOT yet edited)**
   - Lines 45–52 constants include `NEAR_ANCESTOR_MAX = 2` and `JURIS`.
   - Lines 1133–1149, category loop:
     ```python
     for d in roots.values():
         exact = [e for e in d["evidence"] if e["relation"] in ("exact", "descendant")]
         near = [e for e in d["evidence"] if e["relation"] == "ancestor" and e["distance"] <= NEAR_ANCESTOR_MAX]
         us_mention = bool(d.get("label", {}).get("mentions"))
         us_treatment = bool(d.get("label", {}).get("treatment_mentions"))
         if d["ot_approved"] or exact:
             d["category"] = "APPROVED_AT_SCOPE"
             d["jurisdictions"] = sorted({e["jurisdiction"] for e in exact})
         elif near: ... "APPROVED_BROADER_TERM"
         elif us_treatment: ... "LABEL_ONLY", ["US"]
         else: ... "INVESTIGATIONAL", []
         d["us_label_scope_mention"] = ...
         d["partner_in"] = sorted({" + ".join(...) for rs in approved_sets if d["root"] in rs and len(rs) > 1})
     ```
     - Then EMA, `scope_named_by`, `therapeutic_role` and `approved_tags` are set only when category != INVESTIGATIONAL.
   - Lines 1186–1206: letter loop. It sets `d["scope_first_letter"] = dict(date, application, term, url)` or None.
   - Then `record_dates()` and a `for d in cand:` reference_claims loop, ending around line 1229.
   - Next comes `# ---- fidelity: tiers from independent origin families` (line ~1231). Line 1282 is `if d["category"] != "INVESTIGATIONAL":` for fidelity.
   - Lines 1562–1574:
     ```python
     for d in sorted(roots.values(), key=lambda x: x["name"]):
         byc[d["category"]].append(d)
     ...
     at_scope = byc["APPROVED_AT_SCOPE"]
     us_ok = [d for d in at_scope if "US" in d["jurisdictions"]]
     non_us = [d for d in at_scope if "US" not in d["jurisdictions"]]
     elsewhere_ds = [d for d in byc["APPROVED_BROADER_TERM"] if not d["scope_named_by"]]
     approved_ds = sorted(at_scope + [d for d in byc["APPROVED_BROADER_TERM"] if d["scope_named_by"]] + byc["LABEL_ONLY"], key=lambda x: x["name"])
     inv_ds = sorted(byc["INVESTIGATIONAL"], key=lambda x: (-STAGE_RANK.get(x["best_stage"], 0), x["name"]))
     ```
   - Lines 1598–1602: `branches = [branch("branch:approved", f"Approved ({len(approved_ds)})", <note>, [drug_node(d) for d in approved_ds], dict(source=...))]`.
     - The stage branches come from `inv_ds`.
     - Other branches: notinot, unresolved, subtype, audit, review (review reasons at 1677–1687 use the old categories).
   - Line 1765: meta `excluded_approved_elsewhere`.
   - Lines 1828–1834: regression checks that will break under the new logic and need updating:
     - `decitabine_broader_term_eu`
     - `cedazuridine_broader_term_eu_us_regimen`
     - `venetoclax_label_only`
     - `enasidenib_parent_salt_merge`
   - Line 1862: crisantaspase check.
   - Lines 1942–1949: tests.
     - The approved branch must be in the 3 categories and have `scope_named_by`.
     - Stage-branch drugs must be INVESTIGATIONAL.
     - Every root except `elsewhere_ds` must appear in the tree (line 1949). This needs updating if excluded drugs are dropped.
   - Line 1995: counts. Lines 2013–2014: rule notes text.

   **Other consumers of category**
   - `tools/central_anchor.py:128`: `only_scope = {d for d, c in cats.items() if c and c != "INVESTIGATIONAL"}`.
   - `public/disease.html:100-153`: category help text and colours for APPROVED_AT_SCOPE, BROADER_TERM, LABEL_ONLY, INVESTIGATIONAL.
   - `tools/aml_pathways.py`: line 28 `CPX = [("DAUNORUBICIN","DAUNORUBICIN"),("CYTARABINE","CYTARABINE")]`; line 65 sets the CPX-351 regimen as "approved" for t-AML/AML-MRC. Regimen categories are separate: APPROVED_REGIMEN_US_LABEL, STANDARD_OF_CARE, INVESTIGATIONAL_REGIMEN.

4. Errors and fixes:
   - No new errors this segment.
   - Earlier lessons:
     - Use PYTHONIOENCODING.
     - Use raw strings in patch scripts and grep for 0x08 bytes afterwards.
     - Do not bypass hemonc's Cloudflare block.
     - The Playwright MCP fails with CONNECT_TIMEOUT.

5. Problem Solving:

   **Planned US-AML gate (my design; not yet coded)**
   - Insert it after the reference_claims loop (~line 1229), before "# ---- fidelity".
   - Rules, in order:
     ```python
     d["source_category"] = d["category"]; d["reference_jurisdictions"] = sorted(set(d["jurisdictions"]) - {"US"})
     us_label = bool((d.get("label") or {}).get("treatment_mentions")); us_letter = bool(d.get("scope_first_letter"))
     role = ((d.get("therapeutic_role") or {}).get("tag") or "")
     if name in SUPPORTIVE_DRUGS or role.startswith(("supportive","conditioning")): gate="SUPPORTIVE_OR_CONDITIONING"
     elif name in BACKBONE_DRUGS: gate="BACKBONE_CHEMO"
     elif us_label and us_letter: gate="US_AML_APPROVED"   -> category APPROVED_AT_SCOPE, jurisdictions ["US"]
     elif d["partner_in"] and source_category != "INVESTIGATIONAL": gate="APPROVED_PARTNER_ONLY"  (decitabine)
     elif source_category != "INVESTIGATIONAL": gate="EXCLUDED_NOT_US_AML"  (arsenic, tretinoin, histamine, tagraxofusp; APL/BPDCN/non-US)
     else: stays INVESTIGATIONAL
     ```
   - Curated sets:
     - `BACKBONE_DRUGS`: CYTARABINE, DAUNORUBICIN, IDARUBICIN, MITOXANTRONE, DOXORUBICIN, THIOGUANINE, ETOPOSIDE, FLUDARABINE.
     - `SUPPORTIVE_DRUGS`: SARGRAMOSTIM, FILGRASTIM, PEGFILGRASTIM, LENOGRASTIM, MELPHALAN, THIOTEPA, POSACONAZOLE, METRONIDAZOLE.
   - Expected core approved list: AZACITIDINE, CEDAZURIDINE, ENASIDENIB, GEMTUZUMAB OZOGAMICIN, GILTERITINIB, GLASDEGIB, IVOSIDENIB, MIDOSTAURIN, OLUTASIDENIB, QUIZARTINIB, REVUMENIB, VENETOCLAX, ZIFTOMENIB.
   - Placement:
     - Supplemental (backbone, supportive, partner) drugs go in a new separate "Supplemental" branch, referenced only via approved regimens.
     - Excluded drugs are dropped from the tree and listed in meta with a reason.
     - EU/JP evidence is kept only as reference attrs (`reference_jurisdictions`, `ema_indications`) for US-approved drugs.
   - CPX-351 stays as a pathway regimen in `aml_pathways`.
   - Downstream updates needed:
     - `approved_ds`, `us_ok`/`non_us`, and the branch note.
     - Regression checks 1828–1834; tests 1942–1949; counts; rule notes.
     - `disease.html` help/colour for new categories.
     - `central_anchor` `only_scope`.
   - After edits, rerun the pipeline and check that `attach_pathways` reports no unlinked regimens.

6. All user messages:
   - "check anchor run bzkr2t8ea" (sent twice, earlier).
   - "outline the bottlenecks that were hit and if any of the new logic was successfully replicated across samples that I did not manually specify"
   - "what is the soruce of truth used to group drugs into the approved category. need this to atelast be high fidelity before debugging other issues. expand it to as many as possible and I will input human cirtiera for inclusion or exclusin"
   - "asersenic trioxide is only apprved in APL, which is a rare diesease and should not be consdiered within this network. other regulators should not be factored in directly, only as reference populations in supplemental tables if also approved in the US. supportive care drugs should be consdiered as supplemental regimens, not direct treaments. spportive care or conditioning drugs should only be referenced as part of the tratment that the original drug is used in. these treatments (ie generic 7 + 3 regimens) shoudl be referenced only when in combination with my approval drug pathway. make these adjustments to specify downt he list to avooid false positives. I will eventually build out a chart for CLL, APL, etc but do not want them contambinating the AML table"
   - **Constraints still in force:**
     - Never read or cat `.env`.
     - Never output the openFDA API key.
     - No user data in URLs.
     - Never overwrite the user's label files.
     - `rm -rf` is blocked.
     - No CAPTCHA or Cloudflare bypass (hemonc is blocked).
     - Do not change system settings.
     - Caveman terse style, targeted edits (never rewrite a whole file), no README or summary files.
     - Label claims as verified, inferred or unverified.
     - Label the shell in commands given to the user.
     - Suggest `/clear` at task boundaries.
     - Ask one question if a request is ambiguous.
     - Use raw strings in patch scripts and check for 0x08 bytes.

7. Pending Tasks:
   - Implement the US-AML gate in `export_disease_tree.py` per the user's criteria:
     - US FDA AML approval required.
     - APL, CLL and BPDCN excluded.
     - Non-US regulators only as reference.
     - Supportive/conditioning and generic 7+3 backbone drugs only as supplemental, referenced within approved-drug regimens.
   - Update downstream consumers: tree branches, meta, regression checks, tests, `disease.html`, `central_anchor` scope.
   - Back up files to $SP before patching.
   - Rerun the pipeline: central_anchor if needed, then label_s14, then reattach.
   - Verify the approved list matches the expected 13 drugs and pathways stay linked.
   - Report the result to the user, and note the decision that daunorubicin/cytarabine (CPX-351) are treated as backbone while the CPX-351 regimen stays in the pathway.

8. Current Work:
   - I had just read `export_disease_tree.py` regions:
     - Approved branch at 1596–1606.
     - grep of `excluded_approved_elsewhere|elsewhere_ds|non_us` (lines 1568, 1571, 1572, 1765, 1862, 1944, 1949, 1995–1996).
     - Constants 45–52.
   - This was to plan where to insert the gate and the new supplemental branch. No file edits have been made yet.
   - The last status line to the user was: "Adding US-AML gate to category logic in `export_disease_tree.py`."

9. Optional Next Step:
   - Back up `tools/export_disease_tree.py` to $SP.
   - Write a patch script (raw strings, assert count==1) that:
     - Adds the `BACKBONE_DRUGS` / `SUPPORTIVE_DRUGS` constants near line 51.
     - Inserts the US-AML gate after the reference_claims loop (before "# ---- fidelity").
     - Adds a "Supplemental" branch and an excluded-drugs meta list.
     - Updates the regression checks and tests.
   - Then rerun and verify. This directly continues the user's instruction: "make these adjustments to specify downt he list to avooid false positives."
</summary>

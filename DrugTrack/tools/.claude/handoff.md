<analysis>
This section restates the earlier conversation summary, then adds what happened since:
- I patched the AML-only event gate into `central_anchor.py`.
- I tested it on venetoclax (matched the curated chain).
- I moved the old outputs aside and ran the full set: 17 of 23 drugs anchored.
- I reviewed per-drug results and found issues: revumenib's original approval dropped, and midostaurin's RATIFY was missed while pediatric "not established" studies were credited.
- I was reading `scan()` to fix these when the summary was requested.
</analysis>

<summary>
1. Primary Request and Intent:

   **Governing AML request**
   - Reclassify each drug's central study, using the FDA approval package as the source of truth.
   - Use the start-year sort as validation.
   - A drug should have no live central trial unless an exclusion applies.
   - List approved-regimen combinations separately from other combinations.
   - AML only; CLL comes later.

   **Earlier requests (done)**
   - DDI/probe studies go in their own supplemental group, separate from the central trial.
   - Combinations of approved drugs are grouped under the FDA-anchored central trial.
   - Rerun everything and print http://localhost:4300/disease.

   **Latest direction (verbatim)**
   - "I want to work within the AML treatment diagram first, try to get the labeling right without having to link to previous separate approvals"
   - Clarifying answer: "AML approvals only".
   - Meaning:
     - Only FDA approval events that added an AML indication count.
     - Central trial = lead trial of the first AML approval.
     - Later AML approvals become approved-regimen groups.
     - Per-approval dates are kept.

2. Key Technical Concepts:

   **Running things**
   - Working directory: `$HOME/Clinical Tracker/DrugTrack`.
   - Python: `PYTHONIOENCODING=utf-8 "$HOME/Clinical Tracker/.venv/Scripts/python.exe"`.
   - Pipeline: `tools/match_pipeline.py run` (about 740s), then scratchpad `reattach.py`. reattach.py rebuilds tree.json and runs attach_semantic and attach_lineage, because AACT auth fails.
   - Viewer: `src/serveViz.ts` at http://localhost:4300/disease.

   **Data structures**
   - `approval_anchor.events_for(apps)` returns events with `docs` keyed by "Label", "Letter" or "Review".
   - Verified: Letter docs exist for 75 events across the 26 drugs.
   - `fetch_doc(url, app)` returns `(path, txt, status)`.
   - tree.json drug nodes: name in `n["name"]`, category in `attrs.category`.

   **Patch hygiene**
   - Use raw-string patch scripts in the scratchpad.
   - After patching, run `grep -c $'\x08' file`; expect 0.

   **Scratchpad**
   - Path: `$TEMP/claude/C--Users-jwrup-Clinical-Tracker/e9a10f28-7091-42b1-af51-3a05c62b52fa/scratchpad`.

3. Files and Code Sections:

   **`tools/central_anchor.py`, patched with the AML gate**
   - Patch script: scratchpad `patch_aml_gate.py`. Backup before the full run: scratchpad `central_anchor.pre_full.py`.
   - Added after MONO_RX:
     ```python
     SCOPE_RX = re.compile(r"acute myel\w* leuk|\bAML\b|acute non-?lymphocytic leuk|\bANLL\b|acute promyelocytic leuk|\bAPL\b", re.I)
     ```
   - Added before `credited, ev_log, ids_cache = {}, [], {}`:
     - For each event, `verdict` is "aml" if any Letter text matches SCOPE_RX.
     - With no letter, the verdict comes from announcements within 7 days.
     - With neither, the verdict is "unknown".
     - The gate log stores `basis` (letter, announcement or none).
     - `evs` is filtered to AML events and prints `"<drug> AML events X of Y unknown Z"`.
     - With no AML events, the status is `no_aml_event` and `event_gate=gate` is written.
   - `event_gate=gate` was added to both the no_candidate_named and anchored outputs.
   - The screen, walk and first_approval logic now use the AML-only evs.
   - `scan()` (lines 256–283):
     - Finds NCT IDs, plus STUDY_TOKEN_RX tokens resolved through `resolve_token` and `ctgov` ids (exact norm_id match).
     - Role is efficacy_basis unless every context matches PMR_RX.

   **Outputs**
   - `data/central_anchor.json`: fresh full run, "17 anchored of 23".
   - Moved to the scratchpad:
     - old pilots (`central_anchor.pilots.bak.json`)
     - old-logic partial (`central_anchor.oldlogic.partial.json`)
     - venetoclax-only test (`central_anchor.venetoclax_only.json`)
   - Log: scratchpad `anchor_aml.log`.

   **`data/curated/approval_lineage.json`**
   - Venetoclax chain: AML steps M14-358 2018-11-21 (lead NCT02203773, co_basis NCT02287233) and VIALE-A 2020-10-16.
   - Midostaurin: RATIFY NCT00651261, 2017-04-28, aliases A2301 / CPKC412A2301 / RATIFY.

4. Errors and fixes:
   - **Earlier run killed at the 30-min limit.** Fixed by limiting scope to the 26 approved drugs.
   - **Scope filter first gave 0 drugs.** Fixed by reading `n["name"]`.
   - **Alpelisib false anchor.** Excluded by the scope filter.
   - **Gate issues found (not yet fixed):**
     - **Revumenib:** the ORIG 2024-11-15 letter was marked non_aml. Likely cause: it says "acute leukemia with KMT2A translocation" without naming AML. Planned fix: add `\bacute leukemias?\b` to SCOPE_RX. The anchor is the same trial (NCT04065399), but first_approval is wrong (2025-10-24).
     - **Midostaurin:** RATIFY NCT00651261 is a MATCH pair but was not credited at ORIG 2017. ORIG labels and reviews were scanned with no hits, so the "Study A2301"/CPKC412A2301 token resolution likely fails. Instead, the 2020-11-24 label's Pediatric Use 8.4 studies were credited as efficacy_basis: NCT03591510 (lead) and NCT00866281. That section says "safety and effectiveness ... not established". Need a negative-context rule ("not established" / Pediatric Use) so these are not efficacy_basis. Also need to debug why the A2301 alias does not resolve: the review may write it differently, or resolve_token or ctgov ids may lack the curated aliases.
     - **Venetoclax 2018 lead:** the tool picked NCT02287233 (Ph1/2) by PHASE_RANK; curated lead is NCT02203773 (M14-358, Ph1). Reported to the user; not changed.

5. Problem Solving — full-run results (verified from the output):

   | Drug | AML events | Anchors |
   |---|---|---|
   | ARSENIC TRIOXIDE | 2 of 2 | lead NCT00482833 at 2018-01-12, with TRETINOIN; first_approval 2000-09-25 |
   | AZACITIDINE | Onureg 2020-09-01 only | lead NCT01757535 (QUAZAR) |
   | CEDAZURIDINE | SUPPL 2026-05-13 | lead NCT03306264; co_basis NCT04657081 (with venetoclax) |
   | DECITABINE | same Inqovi supplement as CEDAZURIDINE | same as CEDAZURIDINE |
   | CYTARABINE / DAUNORUBICIN | Vyxeos ORIG 2017-08-03, SUPPL 2021-03-30 | lead NCT01696084 |
   | ENASIDENIB | — | NCT01915498 |
   | GEMTUZUMAB OZOGAMICIN | — | 2017: NCT00927498 lead, NCT00091234 co_basis; 2020: lead NCT00372593 (with cytarabine + daunorubicin) |
   | GILTERITINIB | ORIG 2018 only | NCT02421939 (ADMIRAL); 2019 SUPPL letter non_aml |
   | GLASDEGIB | — | NCT01546038 (with cytarabine) |
   | IVOSIDENIB | 2018, 2019, 2022 | NCT02074839 at 2018; NCT03173248 at 2022 (with azacitidine) |
   | OLUTASIDENIB | — | NCT02719574 |
   | QUIZARTINIB | — | NCT02668653 |
   | REVUMENIB | — | NCT04065399 at 2025 only |
   | VENETOCLAX | 2018-11-21 and 2020-10-16 | 4 anchors |
   | ZIFTOMENIB | — | NCT04067336 |

   - no_aml_event: DOXORUBICIN, MELPHALAN, MITOXANTRONE, SARGRAMOSTIM, TAGRAXOFUSP (BPDCN), TRETINOIN. Their AML approvals predate the registry, or their indications are not AML.
   - Unknown verdicts: AZACITIDINE 2007, TAGRAXOFUSP ORIG, TRETINOIN NDA021112, VENETOCLAX 2017-12-20. None affects AML anchors.
   - 23 drugs in output: HISTAMINE, IDARUBICIN and THIOGUANINE have no in-range events.
   - All validations are "verified".

6. All user messages:
   - Original AML reclassification request (governing).
   - "give me the localhost to view this"
   - "what is the immunosigdb localhost from the previous work when I wanted to load diagnosis and relapse signatures into it"
   - "is this all saved down to let me view in the morning"
   - "will this burn all of my claude usage"
   - "ok leave it running overnight but i need to close my laptop"
   - "what is the status of the anchor run and the prrocesses from the previous prompt. outline the anwwers, btotlenecks and new integrations to each of them and output the new localhost"
   - "mvoe forward with the second fix for 1. and print the localhost with the 5 fixes, explaining how you matched the logic for the ones I did not manually specify. good job integrating for step 2, make sure the DDI probe studies are supplementally grouped but separate from the main study. I want to recheck based on that logic and I want to group any combination regimens with thes approved drugs (that now have the data packaed tagged with theproper central study) under these central trials."
   - "why does the run take so much longer now"
   - "I want to work within the AML treatment diagram first, try to get the labeling right without having to link to previous separate approvals"
   - AskUserQuestion answer: "AML approvals only".
   - No user messages since; the later turns were background task notifications.

   **Constraints still in force**
   - Never read or cat `.env`.
   - Never output the openFDA API key.
   - No user data in URLs.
   - Never overwrite the user's label files.
   - `rm -rf` is blocked.
   - No CAPTCHA bypass.
   - Do not change system settings; give the user commands instead.
   - Style: caveman terse, targeted edits, no README or summary files, label claims verified / inferred / unverified, label the shell for user commands, suggest `/clear` at task boundaries.

7. Pending Tasks:
   - Fix the revumenib gate: add `\bacute leukemias?\b` to SCOPE_RX.
   - Fix midostaurin, in two parts:
     - Add a negative-context rule in `scan()` so a hit whose context says "not established" or sits in "Pediatric Use" is not efficacy_basis.
     - Debug why RATIFY (A2301) is not resolved from the ORIG review or label.
   - Rerun the affected drugs, e.g. `tools/central_anchor.py REVUMENIB MIDOSTAURIN`. The pilot mode overwrites OUT with only those drugs, so merge into the full central_anchor.json, or rerun all 26 (the gate letters are now cached, so it is fast).
   - Optionally ask the user about the venetoclax 2018 lead rule (phase vs label order).
   - Rerun match_pipeline and reattach.py, then give http://localhost:4300/disease.
   - Report the final counts.

8. Current Work:
   - The full AML-gated run finished: `wrote ... central_anchor.json 17 anchored of 23`.
   - Reviewing the results found the revumenib and midostaurin issues above.
   - I had just read `scan()` (central_anchor.py lines 256–283) to add a negative-context rule and investigate token resolution for midostaurin's RATIFY.
   - Midostaurin inspection showed:
     - ORIG 2017 docs: two labels (about 62.8k chars each) and reviews (9.8k, 217k, 48k, one HTTP 404). No hits.
     - The 2020 SUPPL label hits come from the 8.4 Pediatric Use "not established" text.

9. Optional Next Step:
   - Patch `central_anchor.py`:
     - Add `|\bacute leukemias?\b` to SCOPE_RX.
     - In `scan()`, set the role to a non-efficacy value (e.g. "not_established") when every context matches `r"not (?:been )?established|Pediatric Use"`.
   - Then check whether "A2301" appears in the cached midostaurin review text and why resolve_token/ctgov ids miss it; curated aliases may need to feed the ids_cache.
   - Rerun all 26 drugs (fast now that letters are cached) and confirm:
     - REVUMENIB first_approval is 2024-11-15.
     - MIDOSTAURIN has lead NCT00651261 at 2017-04-28.
   - Then rerun match_pipeline and reattach.py, and give http://localhost:4300/disease.
</summary>

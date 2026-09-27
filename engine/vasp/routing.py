"""Legal-instrument routing + disclosure-request templates (M5).

Routing rule (master reference, section 4 item 10 + section 10 scope
boundary):

- FIU-IND-registered VASP (Reporting Entity under PMLA; VDA SPs brought
  under the AML framework in March 2023) -> SAHYOG/PMLA. Directly
  actionable: the VASP has statutory record-keeping/disclosure
  obligations and the request travels over the SAHYOG portal.
- India-domiciled but NOT registered -> SAHYOG/PMLA as well, flagged:
  an unregistered domestic operator is itself a PMLA enforcement target,
  so the request doubles as an enforcement lead.
- Foreign, unregistered -> EGMONT (FIU-to-FIU intelligence channel)
  first, with MLAT as the formal escalation for coercive or
  court-admissible measures. Per the reference scope boundary these are
  NOT treated as directly actionable via SAHYOG.
- Stablecoin issuer (Tether/Circle) -> ISSUER_FREEZE: a parallel
  fast-track (address blacklist), run ALONGSIDE the VASP disclosure
  route, never presented as a substitute for it.

Templates are drafts: every rendered request is stamped DRAFT and must
be reviewed by the investigating officer before dispatch.
"""
from dataclasses import dataclass
from enum import Enum

from .directory import VaspRecord


class LegalInstrument(Enum):
    SAHYOG_PMLA = "sahyog_pmla"
    MLAT = "mlat"
    EGMONT = "egmont"
    ISSUER_FREEZE = "issuer_freeze"


@dataclass(frozen=True)
class CaseDetails:
    case_id: str          # SAHYOG case ID / FIR number
    agency: str           # requesting agency
    officer: str          # investigating officer (name + rank)
    wallets: tuple[str, ...]
    tx_hashes: tuple[str, ...]
    date_from: str        # YYYY-MM-DD
    date_to: str          # YYYY-MM-DD
    suspected_offence: str


@dataclass(frozen=True)
class RouteRecommendation:
    vasp: VaspRecord
    instrument: LegalInstrument
    rationale: str
    escalations: tuple[LegalInstrument, ...] = ()
    parallel_fast_track: bool = False


def route_for(vasp: VaspRecord) -> RouteRecommendation:
    """Map an attributed VASP to its legal instrument."""
    if vasp.category == "issuer":
        return RouteRecommendation(
            vasp=vasp,
            instrument=LegalInstrument.ISSUER_FREEZE,
            rationale=(
                f"{vasp.name} is a stablecoin issuer, not a PMLA Reporting "
                "Entity. Address-blacklist requests go directly to the "
                "issuer's law-enforcement channel. This is a PARALLEL "
                "fast-track: run it alongside the VASP disclosure route, "
                "never as a substitute for it."
            ),
            parallel_fast_track=True,
        )
    if vasp.fiu_ind_registered:
        extra = ""
        if vasp.domicile != "IN":
            extra = (f" {vasp.name} is offshore-domiciled but FIU-IND "
                     "registered, so the PMLA route still applies.")
        return RouteRecommendation(
            vasp=vasp,
            instrument=LegalInstrument.SAHYOG_PMLA,
            rationale=(
                f"{vasp.name} is an FIU-IND-registered Reporting Entity. "
                "VDA service providers were brought under the PMLA AML "
                "framework in March 2023 with record-keeping and disclosure "
                f"obligations; route the request via SAHYOG.{extra}"
            ),
        )
    if vasp.domicile == "IN":
        return RouteRecommendation(
            vasp=vasp,
            instrument=LegalInstrument.SAHYOG_PMLA,
            rationale=(
                f"{vasp.name} is India-domiciled but NOT FIU-IND registered. "
                "Route via SAHYOG/PMLA anyway: an unregistered domestic VDA "
                "operator is itself a PMLA enforcement target, so this "
                "request doubles as an enforcement lead. Treat attribution "
                "confidence with extra caution."
            ),
        )
    return RouteRecommendation(
        vasp=vasp,
        instrument=LegalInstrument.EGMONT,
        escalations=(LegalInstrument.MLAT,),
        rationale=(
            f"{vasp.name} is foreign and not FIU-IND registered: not "
            "directly actionable via SAHYOG. Start with the Egmont Group "
            "FIU-to-FIU channel (FIU-IND -> counterpart FIU) for "
            "intelligence; escalate to a formal MLAT request where coercive "
            "measures or court-admissible evidence are needed."
        ),
    )


_TEMPLATES: dict[LegalInstrument, str] = {
    LegalInstrument.SAHYOG_PMLA: """\
DRAFT — requires review by the investigating officer before dispatch.

To: Principal Officer / Compliance Team, {vasp_name}
Via: SAHYOG portal — Case ID {case_id}
Subject: Request for disclosure and preservation of records under the
Prevention of Money Laundering Act, 2002

1. The undersigned, {officer} of {agency}, is investigating {case_id}
   (suspected offence: {suspected_offence}).
2. On-chain analysis attributes the following wallet(s) to an account
   held with {vasp_name}:
     Wallets: {wallets}
     Transactions: {tx_hashes}
     Period: {date_from} to {date_to}
3. As an FIU-IND-registered Reporting Entity, you are requested under
   your PMLA record-keeping and disclosure obligations to furnish:
   (a) KYC particulars of the account holder(s);
   (b) complete transaction history for the above wallets/addresses;
   (c) any linked accounts identified by common KYC or device identifiers.
4. You are further requested to PRESERVE all such records and to place
   a debit freeze on the identified accounts pending further direction,
   to prevent dissipation of suspected proceeds of crime.
5. Please treat this request as confidential and confirm receipt within
   48 hours.

{agency} — {officer} — {case_id}
""",
    LegalInstrument.MLAT: """\
DRAFT — requires review by the investigating officer before dispatch.

MUTUAL LEGAL ASSISTANCE REQUEST (skeleton)

Requesting authority : {agency} ({officer}), India — Case {case_id}
Requested authority  : Competent authority for {vasp_name} ({domicile})
Treaty basis         : Mutual Legal Assistance Treaty / reciprocal
                       arrangement on criminal matters
Offence              : {suspected_offence} (dual criminality to be
                       established in the formal request)

Statement of facts:
  On-chain analysis attributes wallet(s) {wallets} (transactions
  {tx_hashes}, period {date_from} to {date_to}) to an account held with
  {vasp_name}.

Assistance sought:
  (a) identity / KYC records of the account holder;
  (b) transaction history and provenance of funds;
  (c) restraint / freezing of the identified accounts pending
      confiscation proceedings.

This skeleton must be expanded into the full MLAT proforma through the
IS-II Division, Ministry of Home Affairs, before transmission.
""",
    LegalInstrument.EGMONT: """\
DRAFT — requires review by the investigating officer before dispatch.

FIU-TO-FIU INTELLIGENCE REQUEST (Egmont channel)

From: Financial Intelligence Unit - India
To  : Counterpart FIU ({domicile}) — for {vasp_name}
Ref : {case_id} / {agency}

Background:
  Indian law enforcement ({agency}, {officer}) is investigating
  {suspected_offence} (Case {case_id}). On-chain analysis attributes
  wallet(s) {wallets} (transactions {tx_hashes}, {date_from} to
  {date_to}) to {vasp_name}, which is not FIU-IND registered and hence
  not directly reachable via SAHYOG.

Intelligence requested:
  (a) whether the wallets map to accounts held with {vasp_name};
  (b) available KYC / transaction intelligence, subject to your
      domestic dissemination rules;
  (c) point of contact for any follow-on formal (MLAT) request.

This is an intelligence-sharing request; coercive measures, if needed,
will follow through the MLAT channel.
""",
    LegalInstrument.ISSUER_FREEZE: """\
DRAFT — requires review by the investigating officer before dispatch.

To: Law-enforcement channel, {vasp_name}
Subject: Request to blacklist/freeze stablecoin addresses — Case {case_id}

1. {agency} ({officer}) is investigating {suspected_offence}
   (Case {case_id}).
2. The following addresses hold suspected proceeds of crime in
   {vasp_name}-issued tokens and are requested for blacklisting:
     Addresses: {wallets}
     Evidence : {tx_hashes} ({date_from} to {date_to})
3. Parallel VASP disclosure route is being pursued separately through
   the applicable legal instrument; this issuer request is a
   time-sensitive parallel measure, NOT a substitute for it.
4. Please confirm action taken and preserve all related records.

{agency} — {officer} — {case_id}
""",
}


def render_request(vasp: VaspRecord, case: CaseDetails,
                   instrument: LegalInstrument) -> str:
    """Render the per-instrument disclosure-request draft for a VASP + case."""
    return _TEMPLATES[instrument].format(
        vasp_name=vasp.name,
        domicile=vasp.domicile,
        case_id=case.case_id,
        agency=case.agency,
        officer=case.officer,
        wallets=", ".join(case.wallets),
        tx_hashes=", ".join(case.tx_hashes),
        date_from=case.date_from,
        date_to=case.date_to,
        suspected_offence=case.suspected_offence,
    )


def recommend_and_draft(label: str, case: CaseDetails) -> tuple[
        RouteRecommendation, str] | None:
    """End-to-end M5 helper: directory label -> route + drafted request.

    Returns None when the label is not in the directory seed (caller
    should surface "unattributed terminal" rather than guessing).
    """
    from .directory import find_vasp
    vasp = find_vasp(label)
    if vasp is None:
        return None
    rec = route_for(vasp)
    return rec, render_request(vasp, case, rec.instrument)
